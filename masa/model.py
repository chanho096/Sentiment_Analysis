import kobert.pytorch_kobert
import kobert.utils
import masa.utils

import torch
import gluonnlp as nlp
import numpy as np

import os
import logging

DEFAULT_OPTION = {
    "batch_size": 64,
    "num_epochs": 20,

    # Pre-Processing
    "bert_max_len": 64,
    "bert_hidden_size": 768,

    # Training
    "learning_rate": 5e-5,
    "drop_out_rate": 0.5,

    # AdamW
    "warmup_ratio": 0.2,
    "max_grad_norm": 1,

    # Print
    "log_interval": 100,

    # ABSA Option
    "object_text_0": "대상",
    "object_text_1": "측면",
    "ABSA_drop_out_rate": 0.5
}


class BERTDataset(torch.utils.data.Dataset):
    def __init__(self, dataset, sentence_idx, label_idx, bert_tokenizer, max_len, pad=True, pair=False):
        # Tokenization 수행
        transform = nlp.data.BERTSentenceTransform(
            bert_tokenizer, max_seq_length=max_len, pad=pad, pair=pair)
        self.sentence = [transform([record[sentence_idx]]) for record in dataset]
        self.labels = [np.array(record[label_idx], dtype=np.int32) for record in dataset]

    def __getitem__(self, i):
        return self.sentence[i] + (self.labels[i],)

    def __len__(self):
        return len(self.labels)


class ABSAClassifier(torch.nn.Module):
    def __init__(self,
                 bert,
                 sa_classifier=None,
                 hidden_size=768,  # bert hidden size
                 dr_rate_0=None,
                 dr_rate_1=None,
                 ):
        super(ABSAClassifier, self).__init__()
        self.bert = bert
        self.dr_rate_0 = dr_rate_0
        self.dr_rate_1 = dr_rate_1

        if sa_classifier:
            self.classifier_0 = sa_classifier
        else:
            self.classifier_0 = torch.nn.Linear(hidden_size, 2)
        self.classifier_1 = torch.nn.Linear(hidden_size, 3)  # mask 1
        self.classifier_2 = torch.nn.Linear(hidden_size, 3)  # mask 2

        if dr_rate_0:
            self.dropout_0 = torch.nn.Dropout(p=dr_rate_0)
        if dr_rate_1:
            self.dropout_1 = torch.nn.Dropout(p=dr_rate_1)
            self.dropout_2 = torch.nn.Dropout(p=dr_rate_1)

    def forward(self, x, segment_ids, attention_mask, sa=True, absa=True):
        # bert forward
        _, pooler = self.bert(inputs_embeds=x, token_type_ids=segment_ids.long(),
                              attention_mask=attention_mask)

        out_0 = None
        out_1 = None
        out_2 = None

        if sa:  # sentiment analysis
            out_0 = self.dropout_0(pooler) if self.dr_rate_0 else pooler
            out_0 = self.classifier_0(out_0)
            out_0 = torch.nn.functional.softmax(out_0, dim=1)

        if absa:  # aspect-based sentiment analysis
            out_1 = self.dropout_1(pooler) if self.dr_rate_1 else pooler
            out_1 = self.classifier_1(out_1)
            out_1 = torch.nn.functional.softmax(out_1, dim=1)

            out_2 = self.dropout_2(pooler) if self.dr_rate_1 else pooler
            out_2 = self.classifier_2(out_2)
            out_2 = torch.nn.functional.softmax(out_2, dim=1)

        return out_0, out_1, out_2


def calculate_accuracy(x, y):
    max_vals, max_indices = torch.max(x, 1)
    train_acc = (max_indices == y).sum().data.cpu().numpy() / max_indices.size()[0]
    return train_acc


def softmax_cross_entropy_loss(yhat, y):
    logprobs = torch.nn.functional.log_softmax(yhat, dim=1)
    return -(y * logprobs).sum() / yhat.shape[0]


def get_bert_tokenizer(vocab):
    tokenizer = kobert.utils.get_tokenizer()
    bert_tokenizer = nlp.data.BERTSPTokenizer(tokenizer, vocab, lower=False)

    return bert_tokenizer


def gen_attention_mask(token_ids, valid_length):
    attention_mask = torch.zeros_like(token_ids)
    for i, v in enumerate(valid_length):
        attention_mask[i][:v] = 1
    return attention_mask.float()


class ABSAModel:
    """
        사전 학습된 Aspect-Based Sentiment Analysis Model 인터페이스

        ABSA Model 을 파일로부터 불러올 수 있다.

    """
    def __init__(self, ctx="cuda:0", opt=None):
        self._state = False

        # ABSA model
        self.model = None
        self.device = torch.device(ctx)
        if not opt:
            opt = DEFAULT_OPTION.copy()
            opt["batch_size"] = 16
        self.opt = opt

        # KO-BERT model
        self.bert_model = None
        self.vocab = None
        self.bert_embedding = None
        self.bert_tokenizer = None

    def load_kobert(self):
        """
            load KO-BERT model - https://github.com/SKTBrain/KoBERT

            KO-BERT 다운로드 시스템을 이용하여 BERT model 을 불러온다.
        """

        bert_model, vocab = kobert.pytorch_kobert.get_pytorch_kobert_model()
        bert_tokenizer = get_bert_tokenizer(vocab)

        self.bert_model = bert_model.to(self.device)
        self.vocab = vocab
        self.bert_embedding = bert_model.get_input_embeddings().to(self.device)
        self.bert_tokenizer = bert_tokenizer

    def load_empty_bert(self, vocab_path=None):
        """
            load empty BERT model

            KO-BERT 시스템을 이용하지 않고, BERT model 을 생성한다.
            vocab... 파일이 필요하다.
        """
        pass

    def load_model(self, model_path, dr_rate_0=None, dr_rate_1=None):
        """
            load pre-trained ABSA model
            
            model_path 가 None 으로 주어지면, 초기화 상태의 ABSA model 생성
        """

        if model_path and not os.path.isfile(model_path):
            logging.error("Invalid model path")
            return False

        if not self.bert_model:
            logging.error("BERT model needs to be prepared")
            return False

        # create classifier
        model = ABSAClassifier(self.bert_model, hidden_size=self.opt["bert_hidden_size"],
                               dr_rate_0=dr_rate_0, dr_rate_1=dr_rate_1).to(self.device)

        # load model parameter
        if model_path is not None:
            model.load_state_dict(torch.load(model_path, map_location=self.device))

        # ready to analyze
        self.model = model
        self._state = True

        return True

    def tokenize(self, corpus_list):
        """
            tokenization with sentence-piece tokenizer (KO-BERT)

            ##### parms info #####
            corpus_list: list type - string set (input of tokenizer)
        """
        if not self._state:
            logging.error("ABSAModel has not been initialized")
            return None

        transform = nlp.data.BERTSentenceTransform(
            self.bert_tokenizer, max_seq_length=self.opt["bert_max_len"], pad=True, pair=False)

        sentence_info = []
        for corpus in corpus_list:
            sentence = transform([corpus])
            """
                ##### Transform Result #####
                sentence[0]: token_ids, numpy array
                sentence[1]: valid_length, array type scalar
                sentence[2]: segment_ids, numpy array
            """
            sentence_info.append(sentence)

        return sentence_info

    def word_embedding(self, sentence_info, corpus_list=None):
        """
            perform word-embedding process using BERT embedding layer

            ##### parms info #####
            sentence_info: result of tokenized corpus (output of tokenization method)
            corpus_list: if sentence_list is None, create sentence_info using corpus_list
        """
        if not self._state:
            logging.error("ABSAModel has not been initialized")
            return None

        if sentence_info is None:
            if corpus_list is None:
                logging.error("Either sentence or corpus is required")
                return None
            sentence_info = self.tokenize(corpus_list)

        # create tensor of tokens
        token_list = []
        for sentence in sentence_info:
            token_list.append(sentence[0])

        token_ids = torch.tensor(token_list).long().to(self.device)

        # word embedding
        x = self.bert_embedding(token_ids)
        x = x.detach().cpu().numpy()

        return x

    def analyze(self, sentence_info, sa=True, absa=False, batch_size=None):
        """
            perform aspect-based sentiment analysis

            ##### parms info #####
            x: tensor of embedded word
            sa: whether to perform sentiment analysis
            absa: whether to perform aspect-based sentiment analysis
            batch_size: evaluation batch size
        """
        if not self._state:
            logging.error("ABSAModel has not been initialized")
            return None

        if batch_size is None:
            batch_size = self.opt["batch_size"]

        # create batch loader
        total_count = len(sentence_info)
        batch_loader = torch.utils.data.DataLoader(sentence_info, batch_size=batch_size, num_workers=0)

        # evaluation
        model = self.model
        device = self.device

        result_0 = np.zeros((total_count, 2), dtype=float) if sa else None
        result_1 = np.zeros((total_count, 3), dtype=float) if absa else None
        result_2 = np.zeros((total_count, 3), dtype=float) if absa else None
        index = 0

        model.eval()
        with torch.no_grad():
            for batch_id, (token_ids, valid_length, segment_ids) in enumerate(batch_loader):
                batch_count = token_ids.shape[0]

                # create tensor of sentence information
                token_ids = token_ids.long().to(device)
                segment_ids = segment_ids.long().to(device)
                valid_length = valid_length

                # get word embedding
                attention_mask = gen_attention_mask(token_ids, valid_length)
                x = self.bert_embedding(token_ids)

                # forward propagation
                out_0, out_1, out_2 = model(x, segment_ids, attention_mask, sa=sa, absa=absa)

                # result
                if sa:
                    result_0[index:index+batch_count] = out_0.cpu().numpy()

                if absa:
                    result_1[index:index+batch_count] = out_1.cpu().numpy()
                    result_2[index:index+batch_count] = out_2.cpu().numpy()

                index = index + batch_count

        """
            ##### Analysis Result #####
            result_0: SA rate
            result_1: ABSA rate for aspect-1
            result_2: ABSA rate for aspect-2
        """
        return result_0, result_1, result_2

    def analyze_quickly(self, corpus_list, sim_aspects, batch_size=None):
        """
            masking + tokenize + analyze + create result 통합

            말뭉치 데이터를 입력으로 받아서, 결과 행렬을 반환한다.

            ##### parms info #####
            corpus_list: list type - string set (input of tokenizer)
            aspects: list type - string of aspects
            sim_aspects: double list type - similar word dictionary
        """
        if not self._state:
            logging.error("ABSAModel has not been initialized")
            return None

        # masking
        masked_corpus_list, masked_corpus_info = masa.utils.gen_aspect_mask(corpus_list, self.opt, sim_aspects)

        # tokenize
        sentence_info = self.tokenize(masked_corpus_list)

        # aspect-based sentiment analysis
        _, result_1, result_2 = self.analyze(sentence_info, sa=False, absa=True, batch_size=batch_size)

        # create result matrix
        result_matrix = masa.utils.create_result_matrix(result_1, result_2, masked_corpus_info,
                                                        len(corpus_list), len(sim_aspects))

        return result_matrix










