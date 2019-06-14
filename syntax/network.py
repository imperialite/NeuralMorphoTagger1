import ujson as json
import numpy as np

import tensorflow as tf
import keras.backend.tensorflow_backend as kbt

import keras.backend as kb
import keras.layers as kl
import keras.optimizers as kopt
from keras.layers import Layer
from keras import Model
from keras.engine.topology import InputSpec
from keras.callbacks import EarlyStopping

from common.read import read_syntax_infile, process_word, make_UD_pos_and_tag
from common.vocabulary import Vocabulary, FeatureVocabulary, vocabulary_from_json
from common.generate import DataGenerator
from common.common import BEGIN, END, PAD
from common.common import gather_indexes
from common.cells import BiaffineAttention, BiaffineLayer, build_word_cnn
from syntax.common import pad_data, load_elmo, make_indexes_for_syntax
from dependency_decoding import chu_liu_edmonds

from deeppavlov import build_model, configs
from deeppavlov.core.common.params import from_params
from deeppavlov.core.commands.utils import parse_config


class PositionEmbedding(Layer):

    def __init__(self, max_length, dim, **kwargs):
        super(PositionEmbedding, self).__init__(**kwargs)
        self.max_length = max_length
        self.dim = dim
        self.input_spec = InputSpec(min_ndim=2)

    def build(self, input_shape):
        self.kernel = self.add_weight(shape=(self.max_length+1, self.dim),
                                      initializer='glorot_uniform', name='kernel')
        self.built = True

    def call(self, inputs):
        while kb.ndim(inputs) > 2:
            inputs = inputs[...,0]
        positions = kb.cumsum(kb.ones_like(inputs, dtype="int32"), axis=-1) - 1
        positions = kb.maximum(positions, self.max_length)
        answer = kb.gather(self.kernel, positions)
        return answer

    def compute_output_shape(self, input_shape):
        output_shape = tuple(input_shape[:2]) + (self.dim,)
        return output_shape


def load_syntactic_parser(infile):
    with open(infile, "r", encoding="utf8") as fin:
        config = json.load(fin)
    info = {key: config.get(key) for key in ["head_model_params", "dep_model_params",
                                             "head_train_params", "dep_train_params"]}
    embedder = load_elmo()
    parser = StrangeSyntacticParser(embedder=embedder, **info)
    parser.dep_vocab = vocabulary_from_json(config["dep_vocab"])
    parser.head_model = parser.build_head_network(**parser.head_model_params)
    parser.dep_model = parser.build_dep_network(**parser.dep_model_params)
    if "head_model_save_file" in config:
        parser.head_model.load_weights(config["head_model_save_file"])
    if "dep_model_save_file" in config:
        parser.dep_model.load_weights(config["dep_model_save_file"])
    return parser


class StrangeSyntacticParser:

    MAX_WORD_LENGTH = 30

    def __init__(self, embedder=None, use_tags=False,
                 use_char_model=False, max_word_length=MAX_WORD_LENGTH,
                 head_model_params=None, dep_model_params=None,
                 head_train_params=None, dep_train_params=None,
                 char_layer_params=None):
        self.embedder = embedder
        self.use_tags = use_tags
        self.use_char_model = use_char_model
        self.max_word_length = max_word_length
        self.head_model_params = head_model_params or dict()
        self.dep_model_params = dep_model_params or dict()
        self.head_train_params = head_train_params or dict()
        self.dep_train_params = dep_train_params or dict()
        self.char_layer_params = char_layer_params or dict()
        self.head_model_params["char_layer_params"] = self.char_layer_params
        self.dep_model_params["char_layer_params"] = self.char_layer_params

    def _initialize_position_embeddings(self):
        new_weight = np.eye(128, dtype="float") / np.sqrt(128)
        new_weight = np.concatenate([new_weight, [[0] * 128]], axis=0)
        layer: kl.Layer = self.head_model.get_layer(name="pos_embeddings")
        layer.set_weights([new_weight])
        return

    def build_head_network(self, use_lstm=True, lstm_size=128, state_size=384,
                           activation="relu", char_layer_params=None,
                           tag_embeddings_size=64):
        if self.embedder is not None:
            word_inputs = kl.Input(shape=(None, self.embedder.dim), dtype="float32")
            word_embeddings = word_inputs
        else:
            word_inputs = kl.Input(shape=(None, self.max_word_length + 2), dtype="float32")
            char_layer_params = char_layer_params or dict()
            word_embeddings = build_word_cnn(word_inputs, from_one_hot=False,
                                             symbols_number=self.symbol_vocabulary.symbols_number_,
                                             char_embeddings_size=32, **char_layer_params)
        inputs = [word_inputs]
        if self.use_tags:
            tag_inputs = kl.Input(shape=(None, self.tag_vocabulary.symbol_vector_size_), dtype="float32")
            tag_embeddings = kl.Dense(tag_embeddings_size, activation="relu")(tag_inputs)
            inputs.append(tag_inputs)
            word_embeddings = kl.Concatenate()([word_embeddings, tag_embeddings])
        if use_lstm:
            projected_inputs = kl.Bidirectional(kl.LSTM(units=lstm_size, return_sequences=True))(word_embeddings)
        else:
            projected_inputs = kl.Dense(256, activation="tanh")(word_embeddings)
        position_embeddings = PositionEmbedding(max_length=128, dim=128, name="pos_embeddings")(projected_inputs)
        embeddings = kl.Concatenate()([projected_inputs, position_embeddings])
        head_states = kl.Dense(state_size, activation=activation)(embeddings)
        dep_states = kl.Dense(state_size, activation=activation)(embeddings)
        attention = BiaffineAttention(state_size)([head_states, dep_states])
        attention_probs = kl.Softmax()(attention)
        model = Model(inputs, attention_probs)
        model.compile(optimizer=kopt.Adam(clipnorm=5.0), loss="categorical_crossentropy", metrics=["accuracy"])
        print(model.summary())
        return model

    def build_dep_network(self, lstm_units=128, state_units=256, dense_units=None,
                          tag_embeddings_size=64, char_layer_params=None):
        dense_units = dense_units or []
        char_layer_params = char_layer_params or dict()
        if self.embedder is not None:
            word_inputs = kl.Input(shape=(None, self.embedder.dim), dtype="float32")
            word_embeddings = word_inputs
        else:
            word_inputs = kl.Input(shape=(None, self.max_word_length + 2), dtype="float32")
            char_layer_params = char_layer_params or dict()
            word_embeddings = build_word_cnn(word_inputs, from_one_hot=False,
                                             symbols_number=self.symbol_vocabulary.symbols_number_,
                                             char_embeddings_size=32, **char_layer_params)
        dep_inputs = kl.Input(shape=(None,), dtype="int32")
        head_inputs = kl.Input(shape=(None,), dtype="int32")
        inputs = [word_inputs, dep_inputs, head_inputs]
        if self.use_tags:
            tag_inputs = kl.Input(shape=(None, self.tag_vocabulary.symbol_vector_size_), dtype="float32")
            tag_embeddings = kl.Dense(tag_embeddings_size, activation="relu")(tag_inputs)
            inputs.append(tag_inputs)
            word_embeddings = kl.Concatenate()([word_embeddings, tag_embeddings])
        if lstm_units > 0:
            word_embeddings = kl.Bidirectional(kl.LSTM(lstm_units, return_sequences=True))(word_embeddings)
        dep_embeddings = kl.Lambda(gather_indexes, arguments={"B": dep_inputs})(word_embeddings)
        head_embeddings = kl.Lambda(gather_indexes, arguments={"B": head_inputs})(word_embeddings)
        dep_states = kl.Dense(state_units, activation=None)(dep_embeddings)
        dep_states = kl.ReLU()(kl.BatchNormalization()(dep_states))
        head_states = kl.Dense(state_units, activation=None)(head_embeddings)
        head_states = kl.ReLU()(kl.BatchNormalization()(head_states))
        state = kl.Concatenate()([dep_states, head_states])
        for units in dense_units:
            state = kl.Dense(units, activation="relu")(state)
        output = kl.Dense(self.dep_vocab.symbols_number_, activation="softmax")(state)
        model = Model(inputs, output)
        model.compile(optimizer=kopt.Adam(clipnorm=5.0), loss="categorical_crossentropy", metrics=["accuracy"])
        print(model.summary())
        return model


    def build_Dozat_network(self, state_units=256, dropout=0.2,
                            lstm_layers=1, lstm_size=128, lstm_dropout=0.2,
                            char_layer_params=None, tag_embeddings_size=64):
        inputs, embeddings = [], []
        if self.embedder is not None:
            word_inputs = kl.Input(shape=(None, self.embedder.dim), dtype="float32")
            inputs.append(word_inputs)
            embeddings.append(word_inputs)
        if self.use_char_model:
            char_inputs = kl.Input(shape=(None, self.max_word_length + 2), dtype="float32")
            char_layer_params = char_layer_params or dict()
            word_embeddings = build_word_cnn(char_inputs, from_one_hot=False,
                                             symbols_number=self.symbol_vocabulary.symbols_number_,
                                             char_embeddings_size=32, **char_layer_params)
            embeddings.append(word_embeddings)
        if self.use_tags:
            tag_inputs = kl.Input(shape=(None, self.tag_vocabulary.symbol_vector_size_), dtype="float32")
            tag_embeddings = kl.Dense(tag_embeddings_size, activation="relu")(tag_inputs)
            inputs.append(tag_inputs)
            embeddings.append(tag_embeddings)
        embeddings = kl.Concatenate()(embeddings) if len(embeddings) > 1 else embeddings[0]
        lstm_input = embeddings
        for i in range(lstm_layers-1):
            lstm_layer = kl.Bidirectional(kl.LSTM(lstm_size, dropout=lstm_dropout, return_sequences=True))
            lstm_input = lstm_layer(lstm_input)
        lstm_layer = kl.Bidirectional(kl.LSTM(lstm_size, dropout=lstm_dropout, return_sequences=True))
        lstm_output = lstm_layer(embeddings)
        # selecting each word head
        head_encodings = kl.Dropout(dropout)(kl.Dense(state_units, activation="relu")(lstm_output))
        dep_encodings = kl.Dropout(dropout)(kl.Dense(state_units, activation="relu")(lstm_output))
        head_similarities = BiaffineAttention(state_units, use_first_bias=True)([dep_encodings, head_encodings])
        head_probs = kl.Softmax(naem="heads", axis=-1)(head_similarities)
        # selecting each word dependency type (with gold heads)
        dep_inputs = kl.Input(shape=(None,), dtype="int32")
        head_inputs = kl.Input(shape=(None,), dtype="int32")
        inputs.extend([dep_inputs, head_inputs])
        dep_embeddings = kl.Lambda(gather_indexes, arguments={"B": dep_inputs})(lstm_output)
        head_embeddings = kl.Lambda(gather_indexes, arguments={"B": head_inputs})(lstm_output)
        dep_encodings = kl.Dropout(dropout)(kl.Dense(state_units, activation="relu")(dep_embeddings))
        head_encodings = kl.Dropout(dropout)(kl.Dense(state_units, activation="relu")(head_embeddings))
        dep_probs = BiaffineLayer(state_units, self.dep_vocab.symbols_number_,
                                  name="deps", use_first_bias=True, use_second_bias=True,
                                  use_label_bias=True, activation="softmax")([dep_embeddings, head_embeddings])
        outputs = [head_probs, dep_probs]
        model = Model(inputs, outputs)
        model.compile(optimizer=kopt.Adam(clipnorm=5.0), loss=["categorical_crossentropy"] * 2,
                      metrics=["accuracy", "accuracy"])
        print(model.summary())
        return model

    def _recode(self, sent):
        if isinstance(sent[0], str):
            sent, from_word = [sent], True
        else:
            from_word = False
        answer = np.full(shape=(len(sent), self.max_word_length+2), fill_value=PAD, dtype="int32")
        for i, word in enumerate(sent):
            word = word[-self.max_word_length:]
            answer[i, 0], answer[i, len(word) + 1] = BEGIN, END
            answer[i, 1:len(word) + 1] = self.symbol_vocabulary.toidx(word)
        return answer[0] if from_word else answer

    def _transform_data(self, sents, to_train=False):
        sents = [[process_word(word, to_lower=True, append_case="first",
                               special_tokens=["<s>", "</s>"]) for word in sent] for sent in sents]
        if to_train:
            self.symbol_vocabulary = Vocabulary(character=True, min_count=3).train(sents)
        sents = [self._recode(sent) for sent in sents]
        return sents

    def _transform_tags(self, sents, to_train=False):
        sents = [['BEGIN'] + sent + ['END'] for sent in sents]
        if to_train:
            self.tag_vocabulary = FeatureVocabulary(min_count=3).train(sents)
        answer = [[self.tag_vocabulary.to_vector(x, return_vector=True) for x in sent] for sent in sents]
        return answer

    def train(self, sents, heads, deps, dev_sents=None, dev_heads=None, dev_deps=None,
              tags=None, dev_tags=None):
        sents, heads, deps = pad_data(sents, heads, deps)
        if self.use_char_model:
            sent_data = self._transform_data(sents, to_train=True)
        else:
            sent_data = sents
        if tags is not None:
            tag_data = self._transform_tags(tags, to_train=True)
        else:
            tag_data = None
        if dev_sents is not None:
            dev_sents, dev_heads, dev_deps = pad_data(dev_sents, dev_heads, dev_deps)
            dev_sent_data = self._transform_data(dev_sents) if self.use_char_model else dev_sents
            dev_tag_data = self._transform_tags(dev_tags) if self.use_tags else None
        else:
            dev_sent_data, dev_heads, dev_deps, dev_tag_data = None, None, None, None
        self.dep_vocab = Vocabulary(min_count=3).train(deps)
        self.train_head_model(sent_data, heads, dev_sent_data, dev_heads,
                              tags=tag_data, dev_tags=dev_tag_data, **self.head_train_params)
        self.train_dep_model(sent_data, heads, deps, dev_sent_data, dev_heads, dev_deps,
                             tags=tag_data, dev_tags=dev_tag_data, **self.dep_train_params)
        return self

    def train_head_model(self, sents, heads, dev_sents, dev_heads, tags=None, dev_tags=None,
                         nepochs=5, batch_size=16, patience=1):
        self.head_model = self.build_head_network(**self.head_model_params)
        # self._initialize_position_embeddings()
        head_gen_params = {"embedder": self.embedder, "batch_size": batch_size,
                           "classes_number": DataGenerator.POSITIONS_AS_CLASSES}
        additional_data = [tags] if self.use_tags else None
        train_gen = DataGenerator(sents, heads, additional_data=additional_data, **head_gen_params)
        if dev_sents is not None:
            additional_data = [dev_tags] if self.use_tags else None
            dev_gen = DataGenerator(dev_sents, dev_heads, additional_data=additional_data,
                                    shuffle=False, **head_gen_params)
            validation_steps = dev_gen.steps_per_epoch
        else:
            dev_gen, validation_steps = None, None
        callbacks = []
        if patience >= 0:
            callbacks.append(EarlyStopping(monitor="val_acc", restore_best_weights=True, patience=patience))
        self.head_model.fit_generator(train_gen, train_gen.steps_per_epoch,
                                      validation_data=dev_gen, validation_steps=validation_steps,
                                      callbacks=callbacks, epochs=nepochs)
        return self

    def train_dep_model(self, sents, heads, deps, dev_sents, dev_heads, dev_deps,
                        tags=None, dev_tags=None, nepochs=2, batch_size=16, patience=1):
        self.dep_model = self.build_dep_network(**self.dep_model_params)
        dep_indexes, head_indexes, dep_codes =\
            make_indexes_for_syntax(heads, deps, dep_vocab=self.dep_vocab, to_pad=False)
        dep_gen_params = {"embedder": self.embedder, "classes_number": self.dep_vocab.symbols_number_,
                          "batch_size": batch_size, "target_padding": PAD,
                          "additional_padding": [DataGenerator.POSITION_AS_PADDING] * 2}
        additional_data = [dep_indexes, head_indexes]
        if self.use_tags:
            additional_data.append(tags)
            dep_gen_params["additional_padding"].append(0)
        train_gen = DataGenerator(sents, targets=dep_codes, additional_data=additional_data, **dep_gen_params)
        if dev_sents is not None:
            dev_dep_indexes, dev_head_indexes, dev_dep_codes = \
                make_indexes_for_syntax(dev_heads, dev_deps, dep_vocab=self.dep_vocab, to_pad=False)
            additional_data = [dev_dep_indexes, dev_head_indexes]
            if self.use_tags:
                additional_data.append(dev_tags)
            dev_gen = DataGenerator(data=dev_sents, targets=dev_dep_codes,
                                    additional_data=additional_data,
                                    shuffle=False, **dep_gen_params)
            validation_steps = dev_gen.steps_per_epoch
        else:
            dev_gen, validation_steps = None, None
        callbacks = []
        if patience >= 0:
            callbacks.append(EarlyStopping(monitor="val_acc", restore_best_weights=True, patience=patience))
        self.dep_model.fit_generator(train_gen, train_gen.steps_per_epoch,
                                     validation_data=dev_gen,
                                     validation_steps=validation_steps,
                                     callbacks=callbacks, epochs=nepochs)
        return self

    def predict(self, data):
        data = pad_data(data)
        head_probs, chl_pred_heads = self.predict_heads(data)
        deps = self.predict_deps(data, chl_pred_heads)
        return chl_pred_heads, deps

    def predict_heads(self, data):
        probs, heads = [None] * len(data), [None] * len(data)
        test_gen = DataGenerator(data, embedder=self.embedder,
                                 yield_targets=False, yield_indexes=True, nepochs=1)
        for batch_index, (batch, indexes) in enumerate(test_gen):
            batch_probs = self.head_model.predict(batch)
            for i, index in enumerate(indexes):
                L = len(data[index])
                curr_probs = batch_probs[i][:L - 1, :L - 1]
                curr_probs /= np.sum(curr_probs, axis=-1)
                probs[index] = curr_probs
                heads[index] = np.argmax(curr_probs[1:], axis=-1)
        chl_pred_heads = [chu_liu_edmonds(elem.astype("float64"))[0][1:] for elem in probs]
        return probs, chl_pred_heads

    def predict_deps(self, data, heads):
        dep_indexes, head_indexes = make_indexes_for_syntax(heads)
        generator_params = {"embedder": self.embedder,
                            "additional_padding": [DataGenerator.POSITION_AS_PADDING] * 2}
        test_gen = DataGenerator(data, additional_data=[dep_indexes, head_indexes],
                                 yield_indexes=True, yield_targets=False, shuffle=False,
                                 nepochs=1, **generator_params)
        answer = [None] * len(data)
        for batch, indexes in test_gen:
            batch_probs = self.dep_model.predict(batch)
            batch_labels = np.argmax(batch_probs, axis=-1)
            for i, index in enumerate(indexes):
                L = len(data[index])
                curr_labels = batch_labels[i][1:L - 1]
                answer[index] = [self.dep_vocab.symbols_[elem] for elem in curr_labels]
        return answer


def evaluate_heads(corr_heads, pred_heads):
    corr, total, corr_sents = 0, 0, 0
    for corr_sent, pred_sent in zip(corr_heads, pred_heads):
        if len(corr_sent) == len(pred_sent) + 2:
            corr_sent = corr_sent[1:-1]
        if len(corr_sent) != len(pred_sent):
            raise ValueError("Different sentence lengths.")
        has_nonequal = False
        for x, y in zip(corr_sent, pred_sent):
            corr += int(x == y)
            has_nonequal |= (x != y)
        corr_sents += 1 - int(has_nonequal)
        total += len(corr_sent)
    return corr, total, corr / total, corr_sents, len(corr_heads), corr_sents / len(corr_heads)


if __name__ == "__main__":
    parser = load_syntactic_parser("syntax/config/config_load_basic.json")
    test_infile = "/home/alexeysorokin/data/Data/UD2.3/UD_Russian-SynTagRus/ru_syntagrus-ud-test.conllu"
    sents, heads, deps = read_syntax_infile(test_infile, to_process_word=False)
    pred_heads, pred_deps = parser.predict(sents)
    print(evaluate_heads(heads, pred_heads))



