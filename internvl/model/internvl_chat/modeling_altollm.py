import torch
from typing import Optional, List
from internvl.train.constants import IMAGENET_MEAN, IMAGENET_STD
from internvl.conversation import get_conv_template
from .modeling_internvl_chat import InternVLChatModel
from .alto import MaskDecoder

class ALToLLM(InternVLChatModel):
    def __init__(self, config):
        super().__init__(config)
        self.mask_decoder = MaskDecoder.init_model_from_config( 
            model_path=None,
            config_path="./config/alto.yaml",
            need_encoder=True,
            need_decoder=True,
            )
        self.mask_loss_weight = 0

    def forward(
            self,
            pixel_values: torch.FloatTensor,
            input_ids: torch.LongTensor = None,
            attention_mask: Optional[torch.Tensor] = None,
            position_ids: Optional[torch.LongTensor] = None,
            image_flags: Optional[torch.LongTensor] = None,
            past_key_values: Optional[List[torch.FloatTensor]] = None,
            labels: Optional[torch.LongTensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            statistics: Optional[torch.LongTensor] = None,
            loss_weight: Optional[List] = None,
            loss_reduction_all_gather: Optional[bool] = False,
            target_masks: Optional[torch.Tensor] = None,
        ):
        if target_masks is not None:
            input_ids, labels, lengths = self.mask_decoder.replace_titok_tokens_adaptive(input_ids, labels, target_masks)
        outputs = super().forward(
            pixel_values=pixel_values,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            image_flags=image_flags,
            past_key_values=past_key_values,
            labels=labels,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            statistics=statistics,
            loss_weight=loss_weight,
            loss_reduction_all_gather=loss_reduction_all_gather
        )
        logits = outputs.logits
        if target_masks is not None and self.mask_loss_weight > 0:
            image_src = self.convert_image_to_sam_input(pixel_values)
            mask_loss = self.mask_decoder.compute_mask_loss(logits[..., :-1, :].contiguous(), labels[..., 1:].contiguous(), target_masks, image_src=image_src,lengths=lengths)
            outputs.loss += self.mask_loss_weight * mask_loss
        return outputs


    def batch_chat(self, tokenizer, pixel_values, questions, generation_config, num_patches_list=None,
                   history=None, return_history=False, IMG_START_TOKEN='<img>', IMG_END_TOKEN='</img>',
                   IMG_CONTEXT_TOKEN='<IMG_CONTEXT>', verbose=False, image_counts=None):
        return_ids = generation_config.pop('return_ids', False)
        if history is not None or return_history:
            print('Now multi-turn chat is not supported in batch_chat.')
            raise NotImplementedError

        if image_counts is not None:
            num_patches_list = image_counts
            print('Warning: `image_counts` is deprecated. Please use `num_patches_list` instead.')

        img_context_token_id = tokenizer.convert_tokens_to_ids(IMG_CONTEXT_TOKEN)
        self.img_context_token_id = img_context_token_id

        if verbose and pixel_values is not None:
            image_bs = pixel_values.shape[0]
            print(f'dynamic ViT batch size: {image_bs}')

        queries = []
        for idx, num_patches in enumerate(num_patches_list):
            question = questions[idx]
            if pixel_values is not None and '<image>' not in question:
                question = '<image>\n' + question
            template = get_conv_template(self.template)
            template.system_message = self.system_message
            template.append_message(template.roles[0], question)
            template.append_message(template.roles[1], None)
            query = template.get_prompt()

            image_tokens = IMG_START_TOKEN + IMG_CONTEXT_TOKEN * self.num_image_token * num_patches + IMG_END_TOKEN
            query = query.replace('<image>', image_tokens, 1)
            queries.append(query)

        tokenizer.padding_side = 'left'
        model_inputs = tokenizer(queries, return_tensors='pt', padding=True)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        input_ids = model_inputs['input_ids'].to(device)
        attention_mask = model_inputs['attention_mask'].to(device)
        eos_token_id = tokenizer.convert_tokens_to_ids(template.sep.strip())
        generation_config['eos_token_id'] = eos_token_id

        sequences = self.generate(
            pixel_values=pixel_values,
            input_ids=input_ids,
            attention_mask=attention_mask,
            **generation_config
        )

        responses = tokenizer.batch_decode(sequences)
        responses = [response.split(template.sep.strip())[0].strip() for response in responses]

        if return_ids:
            return responses, input_ids, sequences
        else:
            return responses

    def convert_image_to_sam_input(self, src_image, target_size=(1024,1024)):
        img_mean = torch.tensor(IMAGENET_MEAN).view(1,3,1,1).to(src_image.device).to(src_image.dtype)
        img_std = torch.tensor(IMAGENET_STD).view(1,3,1,1).to(src_image.device).to(src_image.dtype)
        src_image = src_image * img_std + img_mean
        src_image = torch.nn.functional.interpolate(src_image, size=target_size, mode='bicubic', align_corners=False)
        return src_image
        
