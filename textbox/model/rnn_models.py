import inspect
import warnings
from typing import Optional, Tuple, Dict, Any

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import AutoModelForSeq2SeqLM
from transformers.utils import ModelOutput
from transformers.modeling_utils import PreTrainedModel
from transformers.configuration_utils import PretrainedConfig
from torch.nn import CrossEntropyLoss
from .abstract_model import AbstractModel


class RNNConfig(PretrainedConfig):
    model_type = "rnn"
    keys_to_ignore_at_inference = ["past_key_values"]
    attribute_map = {"num_attention_heads": "encoder_attention_heads", "hidden_size": "d_model"}

    def __init__(
        self,
        model_name,
        model_class,
        transformer,
        device,
        input_size=768,
        hidden_size=768,
        vocab_size=50265,
        num_layers=1,
        bias=True,
        batch_first=False,
        dropout=0.,
        encoder_bidirectional=True,
        pad_token_id=1,
        eos_token_id=2,
        bos_token_id=0,
        num_labels=3,
        is_encoder_decoder=True,
        decoder_start_token_id=2,
        forced_eos_token_id=2,
        forced_bos_token_id=0,
        **kwargs
    ):
        self.vocab_size = vocab_size
        self.model_name = model_name
        self.model_class = model_class
        self.transformer = transformer
        self.device_ = device
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.dropout = dropout
        self.num_layers = num_layers
        self.bias = bias
        self.batch_first = batch_first
        self.encoder_bidirectional = encoder_bidirectional
        self.decoder_start_token_id = decoder_start_token_id
        super().__init__(
            num_labels=num_labels,
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            is_encoder_decoder=is_encoder_decoder,
            decoder_start_token_id=decoder_start_token_id,
            forced_eos_token_id=forced_eos_token_id,
            **kwargs,
        )

        # ensure backward compatibility for BART CNN models
        if self.forced_bos_token_id is None and kwargs.get("force_bos_token_to_be_generated", False):
            self.forced_bos_token_id = self.bos_token_id
            warnings.warn(
                f"Please make sure the config includes `forced_bos_token_id={self.bos_token_id}` in future versions. "
                "The config can simply be saved and uploaded again to be fixed."
            )


class RNNPretrainedModel(PreTrainedModel):
    config_class = RNNConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _keys_to_ignore_on_load_unexpected = [r"encoder.version", r"decoder.version"]

    def _init_weights(self, module):
        std = self.config.init_std
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, (RNNDecoder, RNNEncoder)):
            module.gradient_checkpointing = value

    @property
    def dummy_inputs(self):
        pad_token = self.config.pad_token_id
        input_ids = torch.tensor([[0, 6, 10, 4, 2], [0, 8, 12, 2, pad_token]], device=self.device)
        dummy_inputs = {
            "attention_mask": input_ids.ne(pad_token),
            "input_ids": input_ids,
        }
        return dummy_inputs


class RNNOutput(ModelOutput):
    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    decoder_hidden_states_before: Optional[torch.FloatTensor] = None
    decoder_cells_before: Optional[torch.FloatTensor] = None
    decoder_hidden_state_last_layer: Optional[torch.FloatTensor] = None
    encoder_last_hidden_state: Optional[torch.FloatTensor] = None
    last_hidden_state: torch.FloatTensor = None
    a_t: Optional[torch.FloatTensor] = None


class RNNEncoder(RNNPretrainedModel):

    def __init__(self, config):
        super(RNNEncoder, self).__init__(config)
        self.model_class = config.model_class
        self.input_size = config.input_size
        self.hidden_size = config.hidden_size
        self.num_layers = config.num_layers
        self.bias = config.bias
        self.batch_first = config.batch_first
        self.dropout = config.dropout
        self.bidirectional = config.encoder_bidirectional
        self.word_embeddings_encoder = config.transformer.model.encoder.embed_tokens
        self.device_ = config.device_
        self.h_projection = nn.Linear(
            in_features=self.hidden_size + self.hidden_size * int(self.bidirectional),
            out_features=self.hidden_size,
            bias=False
        )
        self.c_projection = nn.Linear(
            in_features=self.hidden_size + self.hidden_size * int(self.bidirectional),
            out_features=self.hidden_size,
            bias=False
        )
        self.encoder = self.model_class(
            input_size=self.input_size,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            bias=self.bias,
            batch_first=self.batch_first,
            dropout=self.dropout,
            bidirectional=self.bidirectional
        )
        self.main_input_name = "input_ids"

    def init_hidden(self, batch_size):
        num_bi = 1
        if self.bidirectional:
            num_bi = 2
        h = torch.zeros(num_bi * self.num_layers, batch_size, self.hidden_size)
        c = torch.zeros(num_bi * self.num_layers, batch_size, self.hidden_size)
        return h.to(self.device_), c.to(self.device_)

    def forward(self, input_ids, attention_mask, output_attentions=False, output_hidden_states=False, return_dict=True):
        decoder_cells = None
        embeddings = self.word_embeddings_encoder(input_ids)
        X = torch.nn.utils.rnn.pack_padded_sequence(
            embeddings, attention_mask.sum(dim=1).tolist(), batch_first=True, enforce_sorted=False
        )
        if self.model_class != nn.LSTM:
            # rnn,gru
            h_0, _ = self.init_hidden(attention_mask.shape[0])
            encoder_outputs, encoder_last_hidden_states = self.encoder(X, h_0)
        else:
            # lstm
            h_0, c_0 = self.init_hidden(attention_mask.shape[0])
            encoder_outputs, (encoder_last_hidden_states, encoder_last_cells) = self.encoder(X, (h_0, c_0))
        encoder_outputs, _ = torch.nn.utils.rnn.pad_packed_sequence(encoder_outputs)
        encoder_outputs = encoder_outputs.transpose(0, 1)
        decoder_hidden_states = encoder_last_hidden_states.transpose(0, 1).reshape(
            attention_mask.shape[0], self.num_layers, -1
        ).transpose(0, 1)
        decoder_hidden_states = self.h_projection(decoder_hidden_states)
        decoder_hidden_states = [t for t in decoder_hidden_states]
        if self.model_class == nn.LSTM:
            encoder_cells = encoder_last_cells.transpose(0, 1).reshape(attention_mask.shape[0], self.num_layers,
                                                                       -1).transpose(0, 1)

            decoder_cells = self.c_projection(encoder_cells)
            decoder_cells = [t for t in decoder_cells]
        return RNNOutput(
            decoder_hidden_states_before=decoder_hidden_states,
            encoder_last_hidden_state=encoder_outputs,
            last_hidden_state=encoder_outputs,
            decoder_hidden_state_last_layer=decoder_hidden_states[-1],
            decoder_cells_before=decoder_cells
        )


class RNNDecoder(RNNPretrainedModel):

    def __init__(self, config):
        super(RNNDecoder, self).__init__(config)
        self.model_name = config.model_name
        self.input_size = config.input_size
        self.hidden_size = config.hidden_size
        self.num_layers = config.num_layers
        self.bias = config.bias
        if self.model_name.lower() == 'gru':
            self.model_class = nn.GRUCell
        elif self.model_name.lower() == 'lstm':
            self.model_class = nn.LSTMCell
        else:
            self.model_class = nn.RNNCell
        self.cells = []
        self.cells.append(
            self.model_class(
                input_size=self.input_size + 2 * self.hidden_size, hidden_size=self.hidden_size, bias=self.bias
            )
        )
        for _ in range(self.num_layers - 1):
            self.cells.append(
                self.model_class(input_size=self.hidden_size, hidden_size=self.hidden_size, bias=self.bias)
            )
        self.cells = nn.ModuleList(self.cells)

    def forward(self, X, hidden, cells=None):
        new_hiddens = []
        new_cells = []
        if self.model_name == 'lstm':
            for i in range(len(self.cells)):
                (new_hidden, new_cell) = self.cells[i](X, (hidden[i], cells[i]))
                X = new_hidden
                new_hiddens.append(new_hidden)
                new_cells.append(new_cell)
        else:
            for i in range(len(self.cells)):
                new_hidden = self.cells[i](X, hidden[i])
                X = new_hidden
                new_hiddens.append(new_hidden)
        return new_hidden, new_hiddens, new_cells


class RNNSeq2Seq(RNNPretrainedModel):

    def __init__(self, config: RNNConfig):
        super(RNNSeq2Seq, self).__init__(config)
        self.config = config
        self.model_name = config.model_name
        self.model_class = config.model_class
        self.input_size = config.input_size
        self.hidden_size = config.hidden_size
        self.vocab_size = config.vocab_size
        self.num_layers = config.num_layers
        self.bias = config.bias
        self.device_ = config.device_
        self.batch_first = config.batch_first
        self.dropout = config.dropout
        self.encoder_bidirectional = config.encoder_bidirectional
        self.pad_token_id = config.pad_token_id
        self.eos_token_id = config.eos_token_id
        self.bos_token_id = config.bos_token_id
        self.transformer = config.transformer

        self.word_embeddings_decoder = self.transformer.model.decoder.embed_tokens
        self.lm_head = self.transformer.lm_head
        self.final_logits_bias = self.transformer.final_logits_bias

        self.loss_fct = CrossEntropyLoss(ignore_index=self.pad_token_id, reduction='mean')
        self.main_input_name = "input_ids"
        # loading encoder and decoder
        self.encoder = RNNEncoder(self.config)
        self.decoder = RNNDecoder(self.config)
        self.att_projection = nn.Linear(
            in_features=self.hidden_size + self.hidden_size * int(self.encoder_bidirectional),
            out_features=self.hidden_size,
            bias=False
        )
        self.output_projection = nn.Linear(
            in_features=self.hidden_size * 2 + self.hidden_size * int(self.encoder_bidirectional),
            out_features=self.hidden_size,
            bias=self.bias
        )

    @staticmethod
    def _expand_inputs_for_generation(
        input_ids: torch.LongTensor,
        expand_size: int = 1,
        is_encoder_decoder: bool = False,
        attention_mask: Optional[torch.LongTensor] = None,
        encoder_outputs: Optional[ModelOutput] = None,
        **model_kwargs,
    ) -> Tuple[torch.LongTensor, Dict[str, Any]]:
        expanded_return_idx = (
            torch.arange(input_ids.shape[0]).view(-1, 1).repeat(1, expand_size).view(-1).to(input_ids.device)
        )
        input_ids = input_ids.index_select(0, expanded_return_idx)

        if "token_type_ids" in model_kwargs:
            token_type_ids = model_kwargs["token_type_ids"]
            model_kwargs["token_type_ids"] = token_type_ids.index_select(0, expanded_return_idx)

        if attention_mask is not None:
            model_kwargs["attention_mask"] = attention_mask.index_select(0, expanded_return_idx)
        if is_encoder_decoder:
            if encoder_outputs is None:
                raise ValueError("If `is_encoder_decoder` is True, make sure that `encoder_outputs` is defined.")
            encoder_outputs["last_hidden_state"] = encoder_outputs.last_hidden_state.index_select(
                0, expanded_return_idx.to(encoder_outputs.last_hidden_state.device)
            )
            encoder_outputs["decoder_hidden_states_before"] = [
                t.index_select(0, expanded_return_idx.to(encoder_outputs.last_hidden_state.device))
                for t in encoder_outputs.decoder_hidden_states_before
            ]

            if encoder_outputs['decoder_cells_before'] is not None:
                encoder_outputs["decoder_cells_before"] = [
                    t.index_select(0, expanded_return_idx.to(encoder_outputs.last_hidden_state.device))
                    for t in encoder_outputs.decoder_hidden_states_before
                ]
            encoder_outputs["decoder_hidden_state_last_layer"
                            ] = encoder_outputs.decoder_hidden_state_last_layer.index_select(
                                0, expanded_return_idx.to(encoder_outputs.last_hidden_state.device)
                            )
            model_kwargs["encoder_outputs"] = encoder_outputs
        return input_ids, model_kwargs

    @staticmethod
    def _update_model_kwargs_for_generation(
        outputs: ModelOutput, model_kwargs: Dict[str, Any], is_encoder_decoder: bool = False
    ) -> Dict[str, Any]:
        # update past
        if "past_key_values" in outputs:
            model_kwargs["past"] = outputs.past_key_values
        elif "mems" in outputs:
            model_kwargs["past"] = outputs.mems
        elif "past_buckets_states" in outputs:
            model_kwargs["past"] = outputs.past_buckets_states
        else:
            model_kwargs["past"] = None

        # update token_type_ids with last value
        if "token_type_ids" in model_kwargs:
            token_type_ids = model_kwargs["token_type_ids"]
            model_kwargs["token_type_ids"] = torch.cat([token_type_ids, token_type_ids[:, -1].unsqueeze(-1)], dim=-1)

        # update attention mask
        if not is_encoder_decoder:
            if "attention_mask" in model_kwargs:
                attention_mask = model_kwargs["attention_mask"]
                model_kwargs["attention_mask"] = torch.cat([
                    attention_mask, attention_mask.new_ones((attention_mask.shape[0], 1))
                ],
                                                           dim=-1)
        model_kwargs['encoder_outputs'] = outputs
        return model_kwargs

    def prepare_inputs_for_generation(
        self, decoder_input_ids, attention_mask=None, use_cache=None, encoder_outputs=None, **kwargs
    ):
        # cut decoder_input_ids if past is used
        if encoder_outputs is not None:
            decoder_input_ids = decoder_input_ids[:, -1]
        return {
            "input_ids": None,  # encoder_outputs is defined. input_ids not needed
            "encoder_outputs": encoder_outputs,
            "decoder_input_ids": decoder_input_ids,
            "attention_mask": attention_mask,
        }

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder

    def attention(self, output_encoder, hidden_decoder, attention_mask):
        enc_hiddens_proj = self.att_projection(output_encoder)
        e_t = torch.bmm(enc_hiddens_proj, hidden_decoder.unsqueeze(-1)).squeeze(-1)
        e_t.data.masked_fill_(attention_mask == 0, -float('inf'))
        alpha_t = F.softmax(e_t, dim=-1)
        a_t = torch.bmm(alpha_t.unsqueeze(1), output_encoder).squeeze(1)
        return a_t

    def shift_tokens_right(self, input_ids: torch.Tensor, pad_token_id: int, decoder_start_token_id: int):
        """
        Shift input ids one token to the right.
        """
        shifted_input_ids = input_ids.new_zeros(input_ids.shape)
        shifted_input_ids[:, 1:] = input_ids[:, :-1].clone()
        shifted_input_ids[:, 0] = decoder_start_token_id
        shifted_input_ids = shifted_input_ids[:, 1:]

        if pad_token_id is None:
            raise ValueError("self.model.config.pad_token_id has to be defined.")
        # replace possible -100 values in labels by `pad_token_id`
        shifted_input_ids.masked_fill_(shifted_input_ids == -100, pad_token_id)

        return shifted_input_ids

    def forward(
        self,
        input_ids,
        attention_mask,
        decoder_input_ids=None,
        encoder_outputs=None,
        labels=None,
        return_dict=True,
        output_attentions=False,
        output_hidden_states=False
    ):
        if decoder_input_ids is None and labels is not None:
            # decoder_input_ids generate if None
            decoder_input_ids = self.shift_tokens_right(labels, self.pad_token_id, self.bos_token_id)

        decoder_cells = None
        if self.model_name == 'lstm':
            # lstm
            masked_lm_loss = None
            if encoder_outputs is None:
                # if encoder outputs has not been computed
                encoder_outputs = self.encoder(input_ids, attention_mask)
            last_hidden_state = encoder_outputs.last_hidden_state
            decoder_hidden_states_before = encoder_outputs.decoder_hidden_states_before
            decoder_cells_before = encoder_outputs.decoder_cells_before
            decoder_hidden_state_last_layer = encoder_outputs.decoder_hidden_state_last_layer
            if labels is not None:
                # train the model
                Y = self.word_embeddings_decoder(decoder_input_ids).transpose(0, 1)
                decoder_hidden_states = decoder_hidden_states_before
                decoder_cells = decoder_cells_before
                decoder_outputs = []
                a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                for y in Y:
                    y_t = torch.cat((y, a_t), dim=-1)
                    decoder_hidden_state_last_layer, decoder_hidden_states, decoder_cells = self.decoder(
                        y_t, decoder_hidden_states, decoder_cells
                    )
                    a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                    output_decoder = self.output_projection(torch.cat((a_t, decoder_hidden_state_last_layer), dim=-1))
                    decoder_outputs.append(output_decoder)
                decoder_outputs = torch.stack(decoder_outputs).transpose(0, 1)
                logits = self.lm_head(decoder_outputs) + self.final_logits_bias.to(self.device_)
                shift_logits = logits.contiguous()
                labels.masked_fill_(labels == -100, self.pad_token_id)
                shift_labels = labels[:, 1:].contiguous()
                masked_lm_loss = self.loss_fct(shift_logits.view(-1, self.vocab_size), shift_labels.view(-1))
            else:
                a_t = encoder_outputs.a_t
                if a_t is None:
                    a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                y = self.word_embeddings_decoder(decoder_input_ids)
                y_t = torch.cat((y, a_t), dim=-1)
                decoder_hidden_state_last_layer, decoder_hidden_states, decoder_cells = self.decoder(
                    y_t, decoder_hidden_states_before, decoder_cells_before
                )
                a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                output_decoder = self.output_projection(torch.cat((a_t, decoder_hidden_state_last_layer), dim=-1))
                logits = self.lm_head(output_decoder.unsqueeze(1)) + self.final_logits_bias.to(self.device_)
        else:
            # gru, rnn
            masked_lm_loss = None
            decoder_hidden_states = None
            if encoder_outputs is None:
                # if encoder outputs has not been computed
                encoder_outputs = self.encoder(input_ids, attention_mask)
            last_hidden_state = encoder_outputs.last_hidden_state
            decoder_hidden_states_before = encoder_outputs.decoder_hidden_states_before
            decoder_hidden_state_last_layer = encoder_outputs.decoder_hidden_state_last_layer
            if labels is not None:
                # train the model
                Y = self.word_embeddings_decoder(decoder_input_ids).transpose(0, 1)
                decoder_hidden_states = decoder_hidden_states_before
                decoder_outputs = []
                a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                for y in Y:
                    y_t = torch.cat((y, a_t), dim=-1)
                    decoder_hidden_state_last_layer, decoder_hidden_states, _ = self.decoder(y_t, decoder_hidden_states)
                    a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                    output_decoder = self.output_projection(torch.cat((a_t, decoder_hidden_state_last_layer), dim=-1))
                    decoder_outputs.append(output_decoder)
                decoder_outputs = torch.stack(decoder_outputs).transpose(0, 1)
                logits = self.lm_head(decoder_outputs) + self.final_logits_bias.to(self.device_)
                shift_logits = logits.contiguous()
                labels.masked_fill_(labels == -100, self.pad_token_id)
                shift_labels = labels[:, 1:].contiguous()
                masked_lm_loss = self.loss_fct(shift_logits.view(-1, self.vocab_size), shift_labels.view(-1))
            else:
                a_t = encoder_outputs.a_t
                if a_t is None:
                    a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                y = self.word_embeddings_decoder(decoder_input_ids)
                y_t = torch.cat((y, a_t), dim=-1)
                decoder_hidden_state_last_layer, decoder_hidden_states, _ = self.decoder(
                    y_t, decoder_hidden_states_before
                )
                a_t = self.attention(last_hidden_state, decoder_hidden_state_last_layer, attention_mask)
                output_decoder = self.output_projection(torch.cat((a_t, decoder_hidden_state_last_layer), dim=-1))
                logits = self.lm_head(output_decoder.unsqueeze(1)) + self.final_logits_bias.to(self.device_)
        outputs = RNNOutput(
            logits=logits,
            loss=masked_lm_loss,
            encoder_last_hidden_state=last_hidden_state,
            decoder_hidden_states_before=decoder_hidden_states,
            last_hidden_state=last_hidden_state,
            a_t=a_t,
            decoder_cells_before=decoder_cells,
            decoder_hidden_state_last_layer=decoder_hidden_state_last_layer
        )
        return outputs


class RNN_Models(AbstractModel):

    def __init__(self, config, tokenizer):
        super().__init__(config, tokenizer)
        self.model_name = config['model_name']
        self.source_max_length = config['src_len']
        self.target_max_length = config['tgt_len']
        # initialize model
        if self.model_name == 'rnn':
            self.model_class = nn.RNN
        elif self.model_name == 'gru':
            self.model_class = nn.GRU
        else:
            self.model_class = nn.LSTM
        params_list = (list(inspect.signature(self.model_class.__init__).parameters.keys()))
        config_dict = {key: val for key, val in config.final_config_dict.items() if key in params_list}

        self.vocab_path = config['vocab_path']
        # load the vocabulary of the transformer
        self.transformer = AutoModelForSeq2SeqLM.from_pretrained(self.vocab_path)
        self.model_config = RNNConfig(
            model_name=self.model_name,
            model_class=self.model_class,
            transformer=self.transformer,
            input_size=config['input_size'],
            hidden_size=config['hidden_size'],
            vocab_size=config['vocab_size'],
            num_layers=config['num_layers'],
            device=self.device,
            bias=config['bias'],
            dropout=config['dropout'],
            encoder_bidirectional=config['encoder_bidirectional'],
            pad_token_id=config['pad_token_id'],
            eos_token_id=config['eos_token_id'],
            bos_token_id=config['bos_token_id'],
            batch_first=config['batch_first'],
            decoder_start_token_id=config['decoder_start_token_id']
        )
        self.model = RNNSeq2Seq(self.model_config)

        self.label_smoothing = config['label_smoothing'] if config['label_smoothing'] else 0.
        # generation settings
        self.generation_kwargs = {}
        self.generation_kwargs['max_length'] = self.target_max_length
        self.generation_kwargs['decoder_start_token_id'] = self.model_config.decoder_start_token_id
        self.generation_kwargs.update(config['generation_kwargs'] or {})

    def forward(self, batch, epoch_idx=-1):
        inputs = {
            'input_ids': batch['source_ids'].to(self.device),
            'attention_mask': batch['source_mask'].to(self.device),
            'labels': batch['target_ids'].to(self.device)
        }
        outputs = self.model(**inputs)

        if self.label_smoothing:
            loss_fct = nn.CrossEntropyLoss(label_smoothing=self.label_smoothing)
            vocab_size = outputs.logits.size()[-1]
            return loss_fct(outputs.logits.view(-1, vocab_size), inputs['labels'][:, 1:].contiguous().view(-1))
        else:
            return outputs.loss

    def generate(self, batch, eval_data, accelerator=None):
        inputs = {
            'input_ids': batch['source_ids'].to(self.device),
            'attention_mask': batch['source_mask'].to(self.device),
        }
        sample_outputs = accelerator.unwrap_model(self.model).generate(**inputs, **self.generation_kwargs)
        sample_outputs = accelerator.pad_across_processes(sample_outputs, dim=1, pad_index=self.tokenizer.pad_token_id)
        sample_outputs = accelerator.gather((sample_outputs))

        decode_kwargs = {'skip_special_tokens': True, 'clean_up_tokenization_spaces': False}
        generated_text = self.tokenizer.batch_decode(sample_outputs, **decode_kwargs)
        generated_text = [g.strip() or 'NULL' for g in generated_text]
        # return generated_text
        return generated_text
