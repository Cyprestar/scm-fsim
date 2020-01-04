import json
from typing import Tuple

import pandas as pd

import torch
from torch.utils.data import Dataset
from torch.utils.data.dataloader import default_collate


class TripletTextDataset(Dataset):
    def __init__(self, text_a_list, text_b_list, text_c_list, label_list=None):
        if label_list is None or len(label_list) == 0:
            label_list = [None] * len(text_a_list)
        assert all(
            len(label_list) == len(text_list)
            for text_list in [text_a_list, text_b_list, text_c_list]
        )
        self.text_a_list = text_a_list
        self.text_b_list = text_b_list
        self.text_c_list = text_c_list
        self.label_list = [0 if label == "B" else 1 for label in label_list]

    def __len__(self):
        return len(self.label_list)

    def __getitem__(self, index):
        text_a, text_b, text_c, label = (
            self.text_a_list[index],
            self.text_b_list[index],
            self.text_c_list[index],
            self.label_list[index],
        )
        return text_a, text_b, text_c, label

    @classmethod
    def from_dataframe(cls, df):
        text_a_list = df["A"].tolist()
        text_b_list = df["B"].tolist()
        text_c_list = df["C"].tolist()
        if "label" not in df:
            df["label"] = "B"
        label_list = df["label"].tolist()
        return cls(text_a_list, text_b_list, text_c_list, label_list)

    @classmethod
    def from_dict_list(cls, data, use_augment=False):
        df = pd.DataFrame(data)
        if "label" not in df:
            df["label"] = "B"
        if use_augment:
            df = TripletTextDataset.augment(df)
        return cls.from_dataframe(df)

    @classmethod
    def from_jsons(cls, json_lines_file, use_augment=False):
        with open(json_lines_file, 'r', encoding="utf-8") as f:
            data = list(map(lambda line: json.loads(line), f))
        return cls.from_dict_list(data, use_augment)

    @staticmethod
    def augment(df):
        df_cp1 = df.copy()
        df_cp1["B"] = df["C"]
        df_cp1["C"] = df["B"]
        df_cp1["label"] = "C"

        df_cp2 = df.copy()
        df_cp2["A"] = df["B"]
        df_cp2["B"] = df["A"]
        df_cp2["label"] = "B"

        df_cp3 = df.copy()
        df_cp3["A"] = df["B"]
        df_cp3["B"] = df["C"]
        df_cp3["C"] = df["A"]
        df_cp3["label"] = "C"

        df_cp4 = df.copy()
        df_cp4["A"] = df["C"]
        df_cp4["B"] = df["A"]
        df_cp4["C"] = df["C"]
        df_cp4["label"] = "C"

        df_cp5 = df.copy()
        df_cp5["A"] = df["C"]
        df_cp5["B"] = df["C"]
        df_cp5["C"] = df["A"]
        df_cp5["label"] = "B"

        df = pd.concat([df, df_cp1, df_cp2, df_cp3, df_cp4, df_cp5])
        df = df.drop_duplicates()
        df = df.sample(frac=1)

        return df


def get_collator(max_len, device, tokenizer):
    def three_pair_collate_fn(batch):
        """
        获取一个mini batch的数据，将文本三元组转化成tensor。

        将ab、ac分别拼接，编码tensor

        :param batch:
        :return:
        """
        example_tensors = []
        for text_a, text_b, text_c, label in batch:
            input_example = InputExample(text_a, text_b, text_c, label)
            a_feature, b_feature, c_feature = input_example.to_two_pair_feature(tokenizer, max_len)
            a_tensor, b_tensor, c_tensor = (
                a_feature.to_tensor(device),
                b_feature.to_tensor(device),
                c_feature.to_tensor(device)
            )
            label_tensor = torch.LongTensor([label]).to(device)
            example_tensors.append((a_tensor, b_tensor, c_tensor, label_tensor))

        return default_collate(example_tensors)

    return three_pair_collate_fn


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids

    def to_tensor(self, device) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        return (
            torch.LongTensor(self.input_ids).to(device),
            torch.LongTensor(self.segment_ids).to(device),
            torch.LongTensor(self.input_mask).to(device),
        )


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, text_a, text_b=None, text_c=None, label=None):
        """Constructs a InputExample.

        Args:
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.text_a = text_a
        self.text_b = text_b
        self.text_c = text_c
        self.label = label

    @staticmethod
    def _text_pair_to_feature(text_a, text_b, tokenizer, max_seq_length):
        tokens_a = tokenizer.tokenize(text_a)
        tokens_b = None

        if text_b:
            tokens_b = tokenizer.tokenize(text_b)
            _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
        else:
            if len(tokens_a) > max_seq_length - 2:
                tokens_a = tokens_a[len(tokens_a) - (max_seq_length - 2):]

        # https://huggingface.co/transformers/model_doc/bert.html?highlight=bertmodel#transformers.BertModel
        # The convention in BERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids: 0   0  0    0    0     0       0 0    1  1  1  1   1 1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids: 0   0   0   0  0     0 0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the wordpiece
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambiguously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.
        tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
        segment_ids = [0] * len(tokens)

        if tokens_b:
            tokens += tokens_b + ["[SEP]"]
            segment_ids += [1] * (len(tokens_b) + 1)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        padding = [0] * (max_seq_length - len(input_ids))
        input_ids += padding
        input_mask += padding
        segment_ids += padding

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        return input_ids, segment_ids, input_mask

    def to_two_pair_feature(self, tokenizer, max_seq_length) -> Tuple[InputFeatures, InputFeatures, InputFeatures]:
        a = self._text_pair_to_feature(self.text_a, None, tokenizer, max_seq_length)
        b = self._text_pair_to_feature(self.text_b, None, tokenizer, max_seq_length)
        c = self._text_pair_to_feature(self.text_c, None, tokenizer, max_seq_length)
        return InputFeatures(*a), InputFeatures(*b), InputFeatures(*c)


def _truncate_seq_pair(tokens_a: list, tokens_b: list, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop(0)
        else:
            tokens_b.pop(0)
