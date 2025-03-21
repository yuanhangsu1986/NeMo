# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import io
import copy
import pathlib
import numpy as np
import torch
import torchvision
from PIL import Image
import webdataset as wds
from typing import Dict, List, Optional, Union

from megatron.core import parallel_state
from omegaconf.omegaconf import DictConfig, ListConfig, open_dict

from nemo.collections.common.parts.preprocessing import manifest
from nemo.collections.common.data.dataset import ConcatDataset

from nemo.collections.multimodal.speech_llm.parts.utils.data_utils import TextProcessing

from nemo.collections.asr.data.audio_to_text_dataset import convert_to_config_list, get_chain_dataset
from nemo.collections.asr.data.audio_to_text import VALID_FILE_FORMATS as VALID_AUDIO_FILE_FORMATS
from nemo.collections.asr.data.audio_to_text import expand_sharded_filepaths
from nemo.collections.asr.parts.preprocessing.features import WaveformFeaturizer

from nemo.core.classes import IterableDataset
from nemo.utils import logging, logging_mode
from nemo.utils.distributed import webdataset_split_by_workers


__all__ = [
    'get_tarred_audio_video_images_text_dataset_from_config',
]

VALID_AUDIO_FILE_FORMATS_SET = set(VALID_AUDIO_FILE_FORMATS.split(';'))
VALID_IMAGE_FILE_FORMATS_SET = {ex for ex, f in Image.registered_extensions().items() if f in Image.OPEN}
VALID_VIDEO_FILE_FORMATS_SET = {'mp4'}


class MediaDataEntity(object):
    """Class for AVLM dataloader instance."""

    def __init__(self, sid, audio_file, video_file, image_files: List[str], duration, context, answer, offset, speaker, orig_sr, lang) -> None:
        """Initialize the AudioTextEntity for a AVLM dataloader instance."""
        self.id = sid
        self.audio_file = audio_file
        self.video_file = video_file
        self.image_files = image_files
        self.duration = duration
        self.context = context
        self.answer = answer
        self.offset = offset
        self.speaker = speaker
        self.orig_sr = orig_sr
        self.lang = lang


class MediaDataset(object):
    """List of audio-transcript text correspondence with preprocessing.

    All of the audio, duration, context, answer are optional.
    If answer is not present, text is treated as the answer.
    """

    def __init__(
        self,
        ids: List[int],
        audio_files: List[str],
        video_files: List[str],
        image_samples: List[List[str]],
        durations: List[float],
        context_list: List[str],
        answers: List[str],
        offsets: List[str],
        speakers: List[Optional[int]],
        orig_sampling_rates: List[Optional[int]],
        langs: List[Optional[str]],
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
        max_number: Optional[int] = None,
        do_sort_by_duration: bool = False,
        index_by_file_id: bool = False,
        max_num_samples: Optional[int] = None,
    ):
        """Instantiates audio-context-answer manifest with filters and preprocessing.

        Args:
            ids: List of examples positions.
            audio_files: List of audio files.
            video_files: List of video files.
            image_samples: List of multiple image names as an sample.
            durations: List of float durations.
            context_list: List of raw text transcripts.
            answers: List of raw text transcripts.
            offsets: List of duration offsets or None.
            speakers: List of optional speakers ids.
            orig_sampling_rates: List of original sampling rates of audio files.
            langs: List of language ids, one for eadh sample, or None.
            min_duration: Minimum duration to keep entry with (default: None).
            max_duration: Maximum duration to keep entry with (default: None).
            max_number: Maximum number of samples to collect.
            do_sort_by_duration: True if sort samples list by duration. Not compatible with index_by_file_id.
            index_by_file_id: If True, saves a mapping from filename base (ID) to index in data.
        """

        data, duration_filtered, num_filtered, total_duration = [], 0.0, 0, 0.0
        if index_by_file_id:
            self.mapping = {}

        for id_, audio_file, video_file, image_files, duration, offset, context, answer, speaker, orig_sr, lang in zip(
            ids, audio_files, video_files, image_samples, durations, offsets, context_list, answers, speakers, orig_sampling_rates, langs
        ):
            # Duration filters.
            if duration is not None:
                curr_min_dur = min(duration) if isinstance(duration, list) else duration
                curr_max_dur = max(duration) if isinstance(duration, list) else duration
                curr_sum_dur = sum(duration) if isinstance(duration, list) else duration
                if min_duration is not None and curr_min_dur < min_duration:
                    duration_filtered += curr_sum_dur
                    num_filtered += 1
                    continue

                if max_duration is not None and curr_max_dur > max_duration:
                    duration_filtered += curr_sum_dur
                    num_filtered += 1
                    continue
                total_duration += curr_sum_dur

            if answer is None:
                duration_filtered += curr_sum_dur
                num_filtered += 1
                continue

            data.append(
                MediaDataEntity(id_, audio_file, video_file, image_files, duration, context, answer, offset, speaker, orig_sr, lang)
            )
            if index_by_file_id and 
                (audio_file is not None or 
                 video_files is not None or 
                 (image_files is not [] and image_files is not None)
                ):
                if audio_file is not None:
                    file_id, _ = os.path.splitext(os.path.basename(audio_file))
                elif video_file is not None:
                    file_id, _ = os.path.splitext(os.path.basename(video_file))
                else:
                    basename = os.path.basename(image_files[0])
                    file_id = basename.split('.', 1)[0]
                if file_id not in self.mapping:
                    self.mapping[file_id] = []
                self.mapping[file_id].append(len(data) - 1)

            # Max number of entities filter.
            if len(data) == max_number:
                break

        if max_num_samples is not None and not index_by_file_id:
            if max_num_samples <= len(data):
                logging.info(f"Subsampling dataset from {len(data)} to {max_num_samples} samples")
                data = data[:max_num_samples]
            else:
                logging.info(f"Oversampling dataset from {len(data)} to {max_num_samples} samples")
                data = data * (max_num_samples // len(data))
                res_num = max_num_samples % len(data)
                res_data = [data[idx] for idx in np.random.choice(len(data), res_num, replace=False)]
                data.extend(res_data)
        elif max_num_samples is not None and index_by_file_id:
            logging.warning("Tried to subsample dataset by max_num_samples, but cannot since index_by_file_id is set.")

        if do_sort_by_duration:
            if index_by_file_id:
                logging.warning("Tried to sort dataset by duration, but cannot since index_by_file_id is set.")
            else:
                data.sort(key=lambda entity: entity.duration)

        logging.info("Dataset loaded with %d files totalling %.2f hours", len(data), total_duration / 3600)
        logging.info("%d files were filtered totalling %.2f hours", num_filtered, duration_filtered / 3600)

        self.data = data

    def __getitem__(self, idx):
        if idx < 0 or idx > len(self.data):
            raise ValueError(f"index out of range [0,{len(self.data)}), got {idx} instead")
        return self.data[idx]

    def __len__(self):
        return len(self.data)


class MediaDataCollection(MediaDataset):
    """`MediaDataset` collector from SpeechLLM json files.

    This collector also keeps backward compatibility with MediaDataset.
    """

    def __init__(
        self,
        manifests_files: Union[str, List[str]],
        context_file: Optional[Union[List[str], str]] = None,
        context_key: str = "context",
        answer_key: str = "answer",
        *args,
        **kwargs,
    ):
        """Parse lists of audio files, image/video files, durations and transcripts texts.

        Args:
            manifests_files: Either single string file or list of such -
                manifests to yield items from.
            *args: Args to pass to `AudioText` constructor.
            **kwargs: Kwargs to pass to `AudioText` constructor.
        """
        self.context_key = context_key
        self.answer_key = answer_key
        self.audio_extension = None
        self.video_extension = None
        # keys: image file's non-extension suffixes which is used for identify different files
        #   with the same extension in a sample
        # value: list of extensions used by the images with the same non-extension suffixes
        # key is an empty string if the image has no non-extension suffix
        # e.g.: {'0001': ['png', 'ppm', 'jpg'], '0002': ['jpg'], ...}
        # e.g.: {'': ['png']}
        self.image_extensions = {}

        (
            ids,
            audio_files,
            video_files,
            image_samples,
            durations,
            context_list,
            answers,
            offsets,
        ) = (
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        )
        speakers, orig_srs, langs = (
            [],
            [],
            [],
        )
        if context_file is not None:
            question_file_list = context_file.split(",") if isinstance(context_file, str) else context_file
            self.context_list = []
            for filepath in question_file_list:
                with open(filepath, 'r') as f:
                    for line in f.readlines():
                        line = line.strip()
                        if line:
                            self.context_list.append(line)
            logging.info(f"Use random text context from {context_file} for {manifests_files}")
        else:
            self.context_list = None

        for item in manifest.item_iter(manifests_files, parse_func=self.__parse_item):
            ids.append(item['id'])
            audio_files.append(item['audio_file'])
            video_files.append(item['video_file'])
            image_samples.append(item['image_sample'])
            durations.append(item['duration'])
            context_list.append(item['context'])
            answers.append(item['answer'])
            offsets.append(item['offset'])
            speakers.append(item['speaker'])
            orig_srs.append(item['orig_sr'])
            langs.append(item['lang'])
        super().__init__(
            ids, audio_files, video_files, image_samples, durations, context_list, answers, offsets, speakers, orig_srs, langs, *args, **kwargs
        )

    def __parse_item(self, line: str, manifest_file: str) -> Dict[str, Any]:
        item = json.loads(line)

        # Audio file
        if 'audio_filename' in item:
            item['audio_file'] = item.pop('audio_filename')
        elif 'audio_filepath' in item:
            item['audio_file'] = item.pop('audio_filepath')
        elif 'audio_file' not in item:
            item['audio_file'] = None

        if item['audio_file'] is not None:
            audio_extension = os.path.splitext(item['audio_file'])[1][1:]
            if self.audio_extension is None:
                self.audio_extension = audio_extension
            elif audio_extension != self.audio_extension:
                # check if the audio file extension is consistent
                item['audio_file'] = None                

        # video file
        if 'video_filename' in item:
            item['video_file'] = item.pop('video_filename')
        elif 'video_filepath' in item:
            item['video_file'] = item.pop('video_filepath')
        elif 'video_files' not in item:
            item['video_file'] = None

        if item['video_file'] is not None:
            video_extension = os.path.splitext(item['video_file'])[1][1:]
            if self.video_extension is None:
                self.video_extension = video_extension
            elif video_extension != self.video_extension:
                # check if the video file extension is consistent
                item['video_file'] = None 

        # image file(s)
        # it could be frames of a video sequence, each with a sequence index as part of its extension:
        # e.g. 0001.1.png, 0001.2.png, ..., 0001.7.png
        # or multiple images serving different purposes as long as their extensions are differnt.
        # e.g. 0001.left_view.png, 0001.right_view.png, 0001.depth_map.png 
        if 'image_filename' in item:
            item['image_sample'] = item.pop('image_filename')
        if 'image_filenames' in item:
            item['image_sample'] = item.pop('image_filenames')
        elif 'image_filepath' in item:
            item['image_sample'] = item.pop('image_filepath')
        elif 'image_sample' not in item:
            item['image_sample'] = None

        # split into a list of images
        if item['image_sample'] is not None:
            item['image_sample'] = item['image_sample'].replace(" ", "").split(',')
            img_exts = set([img[img.find('.')+1:] if img.find('.')!=-1 else '' for img in item['image_sample']])
            if self.image_extensions is {}:
                self.image_extensions = img_exts                    
            elif img_exts != self.image_extensions:
                item['image_sample'] = None

        # Duration.
        if 'duration' not in item:
            item['duration'] = None

        # Answer.
        if self.answer_key in item:
            item['answer'] = item.pop(self.answer_key)
        elif 'text' in item:
            # compatability with ASR manifests that uses 'text' as answer key
            item['answer'] = item.pop('text')
        elif 'text_filepath' in item:
            with open(item.pop('text_filepath'), 'r') as f:
                item['answer'] = f.read()
        else:
            item['answer'] = "na"

        # context.
        if self.context_key in item:
            item['context'] = item.pop(self.context_key)
        elif 'context_filepath' in item:
            with open(item.pop('context_filepath'), 'r') as f:
                item['context'] = f.read()
        elif self.context_list is not None:
            context = np.random.choice(self.context_list).strip()
            item['context'] = context
        elif 'question' in item:
            # compatability with old manifests that uses 'question' as context key
            logging.warning(
                f"Neither `{self.context_key}` is found nor"
                f"`context_file` is set, but found `question` in item: {item}",
                mode=logging_mode.ONCE,
            )
            item['context'] = item.pop('question')
        else:
            # default context if nothing is found
            item['context'] = "what does this audio mean"

        item = dict(
            audio_file=item['audio_file'],
            video_file=item['video_file'],
            image_sample=item['image_sample'],
            duration=item['duration'],
            context=str(item['context']),
            answer=str(item['answer']),
            offset=item.get('offset', None),
            speaker=item.get('speaker', None),
            orig_sr=item.get('orig_sample_rate', None),
            lang=item.get('lang', None),
        )
        return item


class WdsFilter:
    """
    filter function for tarred audio, video, and/or images files, skip entry if not in manifest
    """

    def __init__(self, collection, iterator):
        self.iterator = iterator
        self.collection = collection

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            sample = next(self.iterator)
            key = sample[-1]
            file_id, _ = os.path.basename(key).split('.', 1)
            if file_id in self.collection.mapping:
                return sample
            else:
                logging.warning(f"key not in manifest: {file_id}", mode=logging_mode.ONCE)


class WdsLoopOffsets:
    """
    Loop over wds audio, video, and/or images files
    """

    def __init__(self, collection, iterator):
        self.iterator = iterator
        self.collection = collection
        self.current_key = None
        self.current_others = None      
        self.offset_id = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.current_key is None:
            sample = next(self.iterator)
            self.current_others, self.current_key = sample[:-1], sample[-1]
            self.offset_id = 0
        else:
            fid, _ = os.path.basename(self.current_key).split('.',1)
            offset_list = self.collection.mapping[fid]
            if len(offset_list) == self.offset_id + 1:
                sample = next(self.iterator)
                self.current_others, self.current_key = sample[:-1], sample[-1]
                self.offset_id = 0
            else:
                self.offset_id += 1

        return self.current_others + (self.current_key, self.offset_id)


class MediaCrudeWebDataset(IterableDataset):
    """
    A Dataset which loads webDataset compliant dataset which may have one, two or all of the followings: audio, image and video files.

    Accepts a single comma-separated JSON manifest file containing paths to transcripts and audio files and/or images and/or videos files.
    For audio and video files, the manifest file should also provide offsets and durations (in seconds).
    Each new line is a different sample. Example below:

    .. code-block:: json
        
        SpeechLM:
        {"audio_filepath": "1.wav", "duration": 1.12, "question": "what is the capital of France?", "answer": "Paris"}
        {"audio_filepath": "2.wav", "duration": 2.15, "question": "what is the capital of Italy?", "answer": "Rome"}

        VQA:
        {"video_filepath": "1.mp4", "duration": 1.12, "question": "what is the capital of France?", "answer": "Paris"}
        {"video_filepath": "2.mp4", "duration": 2.15, "question": "what is the capital of Italy?", "answer": "Rome"}

        or 
        {"image_names": "1.1.png,1.2.png.1.3.png,1.4.png,1.5.png,1.6.png,1.7.png", "question": "what is the capital of France?", "answer": "Paris"}
        {"image_names": "2.1.png,2.2.png,2.3.png,2.4.png,2.5.png,2.6.png,2.7.png", "question": "what is the capital of Italy?", "answer": "Rome"}

        3D:
        {"image_names": "1.left_view.png,1.right_view.png,1.depth_map.png", "question": "what is the capital of France?", "answer": "Paris"}
        {"image_names": "2.left_view.png,2.right_view.png,2.depth_map.png", "question": "what is the capital of Italy?", "answer": "Rome"}

    as well as the path(s) to the tarball(s) containing the wav, jpg/*png/mp4 files. Each line of the manifest should
    contain the information for one audio file, one video file or image(s) including at least the transcript and name of the audio
    and the image/video files within the tarball.
    ...

    Valid formats for the media_tar_filepaths argument include:
    (1) a single string that can be brace-expanded, e.g. 'path/to/audio_video_images.tar' or 'path/to/audio_video_images_{1..100}.tar.gz', or
    (2) a list of file paths that will not be brace-expanded, e.g. ['audio_video_images_1.tar', 'audio_video_images_2.tar', ...].

    Note: For brace expansion in (1), there may be cases where `{x..y}` syntax can't be used due to shell.
    This occurs most commonly inside SLURM scripts. Therefore we provide a few equivalent replacements.
    Supported opening braces - { <=> (, [, < and the special tag _OP_.
    Supported closing braces - } <=> ), ], > and the special tag _CL_.
    For SLURM based tasks, we suggest the use of the special tags for ease of use.

    See the WebDataset documentation for more information about accepted data and input formats.

    If using multiple workers the number of shards should be divisible by world_size to ensure an
    even split among workers. If it is not divisible, logging will give a warning but training will proceed.
    In addition, if using mutiprocessing, each shard MUST HAVE THE SAME NUMBER OF ENTRIES after filtering
    is applied. We currently do not check for this, but your program may hang if the shards are uneven!

    Additionally, please note that the len() of this DataLayer is assumed to be the length of the manifest
    after filtering. An incorrect manifest length may lead to some DataLoader issues down the line.

    Args:
        media_tar_filepaths: Either a list of media tarball filepaths, or a
            string (can be brace-expandable).
        manifest_filepath (str): Path to the manifest.
        text_processor: TextProcessing object,
        image_processor: Image processor object,
        sample_rate (int): Sample rate to resample loaded audio to
        int_values (bool): If true, load samples as 32-bit integers. Defauts to False.
        audio_augmentor (nemo.collections.asr.parts.perturb.AudioAugmentor): An AudioAugmentor
            object used to augment loaded audio
        image_augmentor: Image data augmentor,
        shuffle_n (int): How many samples to look ahead and load to be shuffled.
            See WebDataset documentation for more details.
            Defaults to 0.
        min_duration (float): Dataset parameter.
            All training files which have a duration less than min_duration
            are dropped. Note: Duration is read from the manifest JSON.
            Defaults to 0.1.
        max_duration (float): Dataset parameter.
            All training files which have a duration more than max_duration
            are dropped. Note: Duration is read from the manifest JSON.
            Defaults to None.
        blank_index (int): Blank character index, defaults to -1.
        unk_index (int): Unknown character index, defaults to -1.
        normalize (bool): Dataset parameter.
            Whether to use automatic text cleaning.
            It is highly recommended to manually clean text for best results.
            Defaults to True.
        trim (bool): Whether to use trim silence from beginning and end
            of audio signal using librosa.effects.trim().
            Defaults to False.
        bos_id (id): Dataset parameter.
            Beginning of string symbol id used for seq2seq models.
            Defaults to None.
        eos_id (id): Dataset parameter.
            End of string symbol id used for seq2seq models.
            Defaults to None.
        pad_id (id): Token used to pad when collating samples in batches.
            If this is None, pads using 0s.
            Defaults to None.
        shard_strategy (str): Tarred dataset shard distribution strategy chosen as a
            str value during ddp.

            - `scatter`: The default shard strategy applied by WebDataset, where each node gets
              a unique set of shards, which are permanently pre-allocated and never changed at runtime.
            - `replicate`: Optional shard strategy, where each node gets all of the set of shards
              available in the tarred dataset, which are permanently pre-allocated and never changed at runtime.
              The benefit of replication is that it allows each node to sample data points from the entire
              dataset independently of other nodes, and reduces dependence on value of `shuffle_n`.

            :warning: Replicated strategy allows every node to sample the entire set of available tarfiles,
                and therefore more than one node may sample the same tarfile, and even sample the same
                data points! As such, there is no assured guarantee that all samples in the dataset will be
                sampled at least once during 1 epoch. Scattered strategy, on the other hand, on specific
                occasions (when the number of shards is not divisible with ``world_size``), will not sample
                the entire dataset. For these reasons it is not advisable to use tarred datasets as validation
                or test datasets.

        shard_manifests (bool): Whether or not to try / shard manifests. Defaults to False.
        global_rank (int): Worker rank, used for partitioning shards. Defaults to 0.
        world_size (int): Total number of processes, used for partitioning shards. Defaults to 0.

            :note: Below args are NLP-specific

        max_seq_length (int): maximum sequence length for each dataset examples. Examples will either be truncated
            to fit this length or dropped if they cannot be truncated.
        min_seq_length (int): min length of each data example in the dataset. Data examples will be dropped if they
            do not meet the min length requirements.
        tokens_to_generate (int): maximum tokens to generate in a single pass. Defaults to 128.
        context_key: Key to use for the context in your JSONL file
        answer_key: Key to use for the label in your JSONL file
        context_file: Optional[Union[List[str], str]] = None, if provided, will use this file to load
            random questions from, if question is not in manifest.
    """

    def __init__(
        self,
        media_tar_filepaths: Union[str, List[str]],
        manifest_filepath: str,
        text_processor: TextProcessing,
        image_processor,
        sample_rate: int,
        int_values: bool = False,
        audio_augmentor: Optional['nemo.collections.asr.parts.perturb.AudioAugmentor'] = None,
        image_augmentor = None,
        shuffle_n: int = 0,
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
        trim: bool = False,
        shard_strategy: str = "scatter",
        shard_manifests: bool = False,
        global_rank: int = 0,
        world_size: int = 0,
        max_seq_length: int = 1024,
        min_seq_length: int = 1,
        tokens_to_generate: int = 128,
        pad_to_max_length: bool = False,
        context_key: str = 'context',
        answer_key: str = 'answer',
        context_file: Optional[Union[List[str], str]] = None,
    ):
        super().__init__()
        self.text_processor = text_processor
        self.image_processor = image_processor
        self.max_seq_length = max_seq_length
        self.min_seq_length = min_seq_length
        self.is_megatron_iterable = True
        self.shard_manifests = shard_manifests
        self.tokens_to_generate = tokens_to_generate
        self.pad_to_max_length = pad_to_max_length

        self.collection = MediaDataCollection(
            manifests_files=manifest_filepath,
            min_duration=min_duration,
            max_duration=max_duration,
            index_by_file_id=True,
            context_file=context_file,
            context_key=context_key,
            answer_key=answer_key,
        )

        self.len = self._compute_len()

        self.waveform_featurizer = WaveformFeaturizer(sample_rate=sample_rate, int_values=int_values, audio_augmentor=audio_augmentor)
        self.trim = trim

        media_tar_filepaths = expand_sharded_filepaths(
            sharded_filepaths=media_tar_filepaths,
            shard_strategy=shard_strategy,
            world_size=world_size,
            global_rank=global_rank,
        )

        # Put together WebDataset
        self._dataset = wds.WebDataset(urls=media_tar_filepaths, nodesplitter=None)

        if shuffle_n == 0:
            logging.info("WebDataset will not shuffle files within the tar files.")

        # Put together WebDataset pipeline
        # prepare the to_tuple arguments as a string. 
        # If the valid audio, video or image files exist, then their extensions are retrieved from
        # self.collection. If they do not exist, to_tuple will return None
        audio_ext = self.collection.audio_extension or next(iter(VALID_AUDIO_FILE_FORMATS_SET))
        video_ext = self.collection.video_extension or next(iter(VALID_VIDEO_FILE_FORMATS_SET))
        image_exts = ' '.join(self.collection.image_extensions) or \
                        next(iter(VALID_IMAGE_FILE_FORMATS_SET))
        to_tuple_args = ' '.join([audio_ext, video_ext, image_exts, '__key__'])

        self._dataset = wds.DataPipeline(
            wds.SimpleShardList(urls=media_tar_filepaths),
            webdataset_split_by_workers,
            wds.shuffle(shuffle_n),
            wds.tarfile_to_samples(),
            wds.decode('pil'), # only images will be decoded
            wds.to_tuple(to_tuple_args, missing_is_error=False),
            self._filter,
            self._loop_offsets,
            wds.map(self._build_sample),
        )

    def _filter(self, iterator):
        """This function is used to remove samples that have been filtered out by ASRAudioText already.
        Otherwise, we would get a KeyError as _build_sample attempts to find the manifest entry for a sample
        that was filtered out (e.g. for duration).
        Note that if using multi-GPU training, filtering may lead to an imbalance in samples in each shard,
        which may make your code hang as one process will finish before the other.
        """
        return WdsFilter(self.collection, iterator)

    def _loop_offsets(self, iterator):
        """This function is used to iterate through utterances with different offsets for each file."""
        return WdsLoopOffsets(self.collection, iterator)

    def _collate_fn(self, batch):
        # TODO
        return None

    def collate_fn(self, batch):
        # override collate_fn to skip type checking
        return self._collate_fn(batch)

    def _build_sample(self, tup):
        """Builds the training sample by combining the data from the WebDataset with the manifest info."""
        audio_bytes = tup[0]
        video_bytes = tup[1]
        image_pixels = tup[2:-2]
        key = tup[-2]
        offset_id = tup[-1]

        processed_images = []

        if key is not None:
            # Grab manifest entry from self.manifest_preprocessor.collection
            file_id, _ = os.path.basename(key).split('.', 1)
            manifest_idx = self.collection.mapping[file_id][offset_id]
            manifest_entry = self.collection[manifest_idx]

            # init output dict
            output = {"idx": manifest_idx}

            offset = manifest_entry.offset
            if offset is None:
                offset = 0

            # process audio
            if audio_bytes is not None:                
                # Convert audio bytes to IO stream for processing (for SoundFile to read)
                audio_filestream = io.BytesIO(audio_bytes)
                audio_features = self.waveform_featurizer.process(
                    audio_filestream,
                    offset=offset,
                    duration=manifest_entry.duration,
                    trim=self.trim,
                    orig_sr=manifest_entry.orig_sr,
                )
                audio_filestream.close()

                # Audio features
                output["audio_signal"] = audio_features
                output["audio_length"] = torch.tensor(audio_features.shape[0]).long()
            else:
                # dummy audio_features
                output["audio_signal"] = torch.zeros([80])
                # accomodates normalize_batch
                output["audio_length"] = torch.tensor(80)

            # process image
            # TODO: dummy image output
            output["image_sizes"] = None
            for image_pixel in image_pixels:
                if image_pixel is not None:
                    if output["image_sizes"] is None:
                        # TODO: each image has the same size?
                        height = image_pixel.shape[1]
                        width = image_pixel.shape[2]
                        output["image_sizes"].append(torch.tensor([[height, width]], dtype=torch.long))

                    # convert to torch tensor
                    processed_image = torchvision.transforms.functional.pil_to_tensor(image_pixel)
                    
                    # process image
                    if self.image_processor is not None:
                        processed_image = self.image_processor(processed_image)
                    else:
                        processed_image = torch.to_tensor(processed_image).unsqueeze(0)

                    processed_images.append(processed_image)

            if processed_images is not []:
                # concatenate all image tiles along the first dimension
                processed_images = torch.cat(processed_images)
                output["image_signal"] = processed_images
                output["num_image_tiles"] = output["image_signal"].shape[0]

            # TODO: process video. For videos we have to read the raw bytes and deocde it here since 
            # we need to know the offset and the duration

        # Text features
        text_data = self.text_processor(context=manifest_entry.context, output=manifest_entry.answer)

        output.update(text_data)

        if processed_images is not [] or video_bytes is not None:
            output["attention_mask"] = torch.ones(len(text_data), dtype=torch.long)

        output['metadata'] = {
            'audio_filepath': manifest_entry.audio_file,
            'visual_filepaths': manifest_entry.visual_files,
            'offset': offset,
            'duration': manifest_entry.duration,
        }
        return output

    def get_manifest_sample(self, sample_id):
        """
        return manifest item given the index
        """
        return self.collection[sample_id]

    def __iter__(self):
        return self._dataset.__iter__()

    def _compute_len(self):
        # TODO: need to figure out why here needs to be divided by world_size, while in ASR we don't need to.
        if self.shard_manifests and torch.distributed.is_available() and torch.distributed.is_initialized():
            my_len = torch.tensor(len(self.collection), dtype=torch.int32).cuda()
            torch.distributed.all_reduce(my_len)
            my_len = my_len.int() // parallel_state.get_data_parallel_world_size()
            logging.info(f'Sharded manifests: Total length: {my_len}')
        else:
            my_len = len(self.collection) // parallel_state.get_data_parallel_world_size()

        return my_len

    def __len__(self):
        return self.len


def get_media_crude_webdataset(
    config,
    text_processor,
    image_processor,
    audio_augmentor,
    image_augmentor,
    global_rank=0,
    world_size=1,
    shuffle_n=0,
):
    """
    Get media to text webdataset
    """
    media_tar_filepaths = config['media_tar_filepaths']
    manifest_filepaths = config['manifest_filepath']
    datasets = []
    media_tar_filepaths = convert_to_config_list(media_tar_filepaths)
    manifest_filepaths = convert_to_config_list(manifest_filepaths)

    bucketing_weights = config.get('bucketing_weights', None)  # For upsampling buckets
    if bucketing_weights:
        for idx, weight in enumerate(bucketing_weights):
            if not isinstance(weight, int) or weight <= 0:
                raise ValueError(f"bucket weights must be positive integers")

    if len(manifest_filepaths) != len(media_tar_filepaths):
        raise ValueError(
            f"manifest_filepaths (length={len(manifest_filepaths)}) and media_tar_filepaths",
            f"(length={len(media_tar_filepaths)}) need to have the same number of buckets.",
        )

    if 'labels' not in config:
        logging.warning(f"dataset does not have explicitly defined labels")

    if 'max_utts' in config:
        raise ValueError('"max_utts" parameter is not supported for tarred datasets')

    for dataset_idx, (media_tar_filepath, manifest_filepath) in enumerate(
        zip(media_tar_filepaths, manifest_filepaths)
    ):
        if len(media_tar_filepath) == 1:
            media_tar_filepath = media_tar_filepath[0]
        if len(manifest_filepath) == 1:
            manifest_filepath = manifest_filepath[0]

        dataset = MediaCrudeWebDataset(
            media_tar_filepath=media_tar_filepath,
            manifest_filepath=manifest_filepath,
            text_processor=text_processor,
            image_processor=image_processor,
            sample_rate=config['sample_rate'],
            int_values=config.get('int_values', False),
            audio_augmentor=audio_augmentor,
            image_augmentor=image_augmentor,
            shuffle_n=shuffle_n,
            max_duration=config.get('max_duration', None),
            min_duration=config.get('min_duration', None),
            trim=config.get('trim_silence', False),
            shard_strategy=config.get('tarred_shard_strategy', 'scatter'),
            shard_manifests=config.get('shard_manifests', False),
            global_rank=global_rank,
            world_size=world_size,
            max_seq_length=config.max_seq_length,
            min_seq_length=config.min_seq_length,
            tokens_to_generate=config.get('tokens_to_generate', 0),
            pad_to_max_length=config.get('pad_to_max_length', False),
            context_key=config.get('context_key', 'context'),
            answer_key=config.get('answer_key', 'answer'),
            context_file=config.get('context_file', None),
        )

        if bucketing_weights:
            [datasets.append(dataset) for _ in range(bucketing_weights[dataset_idx])]
        else:
            datasets.append(dataset)

    with open_dict(config):  # patch for bucketing tarred datasets
        config['batch_size'] = config.get("micro_batch_size", 1)
    return get_chain_dataset(datasets=datasets, ds_config=config, rank=global_rank)


def get_concat_media_crude_webdataset(
    config,
    text_processor,
    image_processor,
    audio_augmentor,
    image_augmentor,
    global_rank=0,
    world_size=1,
    shuffle_n=0,
):
    """
    Get concat tarred audio to text dataset
    """
    media_tar_filepaths = config['media_tar_filepaths']
    manifest_filepaths = config['manifest_filepath']
    datasets = []
    for dataset_idx, (media_tar_filepath, manifest_filepath) in enumerate(
        zip(media_tar_filepaths, manifest_filepaths)
    ):
        conf = copy.deepcopy(config)
        conf['manifest_filepath'] = manifest_filepath
        conf['media_tar_filepaths'] = media_tar_filepath
        context_files = config.get('context_file', None)
        if isinstance(context_files, ListConfig) and len(context_files) == len(manifest_filepaths):
            conf['context_file'] = context_files[dataset_idx]
        else:
            conf['context_file'] = context_files
        dataset = get_media_crude_webdataset(
            config=conf,
            text_processor=text_processor,
            image_processor=image_processor,
            audio_augmentor=audio_augmentor,
            image_augmentor=image_augmentor,
            shuffle_n=shuffle_n,
            global_rank=global_rank,
            world_size=world_size,
        )
        datasets.append(dataset)

    concat_sampling_probabilities = config.get('concat_sampling_probabilities', None)
    if not isinstance(concat_sampling_probabilities, ListConfig) or len(concat_sampling_probabilities) != len(
        datasets
    ):
        logging.info(
            f"concat_sampling_probabilities is not provided or is not of the same size as datasets,"
            f"using uniform sampling: concat_sampling_probabilities={concat_sampling_probabilities}"
        )
        concat_sampling_probabilities = [1.0 / len(datasets)] * len(datasets)

    dataset = ConcatDataset(
        datasets,
        sampling_technique=config.get('concat_sampling_technique', 'temperature'),
        sampling_temperature=config.get('concat_sampling_temperature', 5),
        sampling_scale=config.get('concat_sampling_scale', 1),
        sampling_probabilities=concat_sampling_probabilities,
        shuffle=config.get('concat_shuffle', True),
        seed=config.get('concat_sampling_seed', None),
        global_rank=global_rank,
        world_size=world_size,
    )
    return dataset


def get_media_crude_webdataset_from_config(
    config: DictConfig,
    text_processor: TextProcessing,
    image_processor,
    audio_augmentor,
    image_augmentor,
    global_rank: int = 0,
    world_size: int = 1,
):
    """
    Get wds dataset from config
    """
    
    is_concat = config.get('is_concat', False)
    if is_concat:
        if 'concat_sampling_technique' in config and config['concat_sampling_technique'] is None:
            logging.warning(
                f"Concat dataset requires `concat_sampling_technique` but it was not provided. Config: {config}"
            )
            return None

    data_parallel_size = parallel_state.get_data_parallel_world_size()
    num_micro_batches = config.global_batch_size // (config.micro_batch_size * data_parallel_size)
    global_batch_size_on_this_data_parallel_rank = num_micro_batches * config.micro_batch_size
    shuffle = config['shuffle']
    shuffle_n = config.get('shuffle_n', 4 * global_batch_size_on_this_data_parallel_rank) if shuffle else 0
    if is_concat:
        dataset = get_concat_media_crude_webdataset(
            config=config,
            text_processor=text_processor,
            image_processor=image_processor,
            audio_augmentor=audio_augmentor,
            image_augmentor=image_augmentor,
            shuffle_n=shuffle_n,
            global_rank=global_rank,
            world_size=world_size,
        )
    else:
        dataset = get_media_crude_webdataset(
            config=config,
            text_processor=text_processor,
            image_processor=image_processor,
            audio_augmentor=audio_augmentor,
            image_augmentor=image_augmentor,
            shuffle_n=shuffle_n,
            global_rank=global_rank,
            world_size=world_size,
        )
    return dataset
