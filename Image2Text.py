import paddle
import paddle.nn as nn
import paddle.nn.functional as F
import paddle.tensor as tensor
from vision_transformer import VisionTransformer, Identity, trunc_normal_, zeros_
from paddle.framework import ParamAttr
from paddle.nn.layer.transformer import _convert_param_attr_to_list
import collections
from paddlenlp.ops import InferTransformerDecoding

class DistilledVisionTransformer(VisionTransformer):
    def __init__(self, img_size=224, patch_size=16, embed_dim=768, depth=12, class_num= 0,
                 num_heads=12, mlp_ratio=4, qkv_bias=True, norm_layer='nn.LayerNorm', epsilon=1e-5,
                 **kwargs):
        super().__init__(img_size=img_size, patch_size=patch_size, embed_dim=embed_dim, depth=depth, class_num= class_num,
                         num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, norm_layer=norm_layer, epsilon=epsilon,
                         **kwargs)
        self.pos_embed = self.create_parameter(
            shape=(1, self.patch_embed.num_patches + 2, self.embed_dim), default_initializer=zeros_)
        self.add_parameter("pos_embed", self.pos_embed)

        self.dist_token = self.create_parameter(
            shape=(1, 1, self.embed_dim), default_initializer=zeros_)
        self.add_parameter("cls_token", self.cls_token)

        trunc_normal_(self.dist_token)
        trunc_normal_(self.pos_embed)
    def forward_features(self, x):
        B = x.shape[0]
        x = self.patch_embed(x)
        
        cls_tokens = self.cls_token.expand((B, -1, -1))
        dist_token = self.dist_token.expand((B, -1, -1))
        x = paddle.concat((cls_tokens, dist_token, x), axis=1)
        
        input_embedding = x + self.pos_embed
        x = self.pos_drop(input_embedding)
        
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)
        
        return x,input_embedding
    
    def forward(self, x):
        x, input_embedding = self.forward_features(x)
        return x, input_embedding
        
class PositionalEmbedding(nn.Layer):
    def __init__(self, emb_dim, max_length,learned = False):
        super(PositionalEmbedding, self).__init__()
        self.emb_dim = emb_dim
        self.position_embeddings = nn.Embedding(num_embeddings=max_length,embedding_dim=self.emb_dim,
            weight_attr=paddle.ParamAttr(initializer=nn.initializer.Normal(0., emb_dim**-0.5)))
        if not learned:
            w = paddle.zeros((max_length, emb_dim),paddle.float32)
            pos = paddle.arange(0, max_length, dtype=paddle.float32).unsqueeze(1)
            div = (-paddle.arange(0, emb_dim, 2,dtype=paddle.float32)/emb_dim * paddle.to_tensor(10000,paddle.float32).log()).exp()
            w[:, 0::2] = paddle.sin(pos * div)
            w[:, 1::2] = paddle.cos(pos * div)
            self.position_embeddings.weight.set_value(w)
            self.position_embeddings.weight.stop_gradient = True
            
    def forward(self, pos):
        return self.position_embeddings(pos)
    
class WordEmbedding(nn.Layer):
    def __init__(self, vocab_size, emb_dim, pad_id=0):
        super(WordEmbedding, self).__init__()
        self.emb_dim = emb_dim
        self.word_embeddings = nn.Embedding(num_embeddings=vocab_size,embedding_dim=emb_dim,padding_idx=pad_id,
            weight_attr=paddle.ParamAttr(initializer=nn.initializer.Normal(0., emb_dim**-0.5)))
    def forward(self, word):
        return self.emb_dim**0.5 * self.word_embeddings(word)
        
class MultiHeadAttention(nn.Layer):
    Cache = collections.namedtuple("Cache", ["k", "v"])
    StaticCache = collections.namedtuple("StaticCache", ["k", "v"])
    def __init__(self,embed_dim,num_heads,dropout=0.,kdim=None,vdim=None,need_weights=False,weight_attr=None,bias_attr=None,**kwargs):
        super(MultiHeadAttention, self).__init__()
        self.embed_dim = embed_dim
        self.kdim = kdim if kdim is not None else embed_dim
        self.vdim = vdim if vdim is not None else embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.need_weights = need_weights
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"
        self.q_proj = nn.Linear(
            embed_dim, embed_dim, weight_attr, bias_attr=bias_attr)
        self.k_proj = nn.Linear(
            self.kdim, embed_dim, weight_attr, bias_attr=bias_attr)
        self.v_proj = nn.Linear(
            self.vdim, embed_dim, weight_attr, bias_attr=bias_attr)
        self.out_proj = nn.Linear(
            embed_dim, embed_dim, weight_attr, bias_attr=bias_attr)

    def _prepare_qkv(self, query, key, value, cache=None):
        q = self.q_proj(query)
        q = tensor.reshape(x=q, shape=[0, 0, self.num_heads, self.head_dim])
        q = tensor.transpose(x=q, perm=[0, 2, 1, 3])
        if isinstance(cache, self.StaticCache):
            k, v = cache.k, cache.v
        else:
            k, v = self.compute_kv(key, value)
        if isinstance(cache, self.Cache):
            k = tensor.concat([cache.k, k], axis=2)
            v = tensor.concat([cache.v, v], axis=2)
            cache = self.Cache(k, v)
        return (q, k, v) if cache is None else (q, k, v, cache)
    def compute_kv(self, key, value):
        k = self.k_proj(key)
        v = self.v_proj(value)
        k = tensor.reshape(x=k, shape=[0, 0, self.num_heads, self.head_dim])
        k = tensor.transpose(x=k, perm=[0, 2, 1, 3])
        v = tensor.reshape(x=v, shape=[0, 0, self.num_heads, self.head_dim])
        v = tensor.transpose(x=v, perm=[0, 2, 1, 3])
        return k, v

    def gen_cache(self, key, value=None, type=Cache):
        if type == MultiHeadAttention.StaticCache:  # static_kv
            k, v = self.compute_kv(key, value)
            return self.StaticCache(k, v)
        elif value is None:  #
            k = tensor.zeros([key.shape[0], self.num_heads, 0, self.head_dim],dtype=key.dtype)
            v = tensor.zeros([key.shape[0], self.num_heads, 0, self.head_dim],dtype=key.dtype)
            return self.Cache(k, v)
        else:
            return self.Cache(key, value)

    def forward(self, query, key=None, value=None, attn_mask=None, cache=None):
        key = query if key is None else key
        value = query if value is None else value
        if cache is None:
            q, k, v = self._prepare_qkv(query, key, value, cache)
        else:
            q, k, v, cache = self._prepare_qkv(query, key, value, cache)
        product = paddle.matmul(
            x=q * (self.head_dim**-0.5), y=k, transpose_y=True)
        if attn_mask is not None:
            product = product + attn_mask
        weights = F.softmax(product)
        if self.dropout:
            weights = F.dropout(
                weights,
                self.dropout,
                training=self.training,
                mode="upscale_in_train")
        out = tensor.matmul(weights, v)
        out = tensor.transpose(out, perm=[0, 2, 1, 3])
        out = tensor.reshape(x=out, shape=[0, 0, out.shape[2] * out.shape[3]])
        out = self.out_proj(out)
        outs = [out]
        if self.need_weights:
            outs.append(weights)
        if cache is not None:
            outs.append(cache)
        return out if len(outs) == 1 else tuple(outs)

class TransformerDecoderLayer(nn.Layer):
    def __init__(self,d_model,nhead,dim_feedforward,dropout=0.0,skdim=None,svdim=None,ckdim=None,cvdim=None,activation='ReLU',
                 attn_dropout=None,act_dropout=None,normalize_before=True,weight_attr=None,bias_attr=None,**kwargs):
        self._config = locals()
        self._config.pop("__class__", None)
        super(TransformerDecoderLayer, self).__init__()
        attn_dropout = dropout if attn_dropout is None else attn_dropout
        act_dropout = dropout if act_dropout is None else act_dropout
        self.normalize_before = normalize_before
        weight_attrs = _convert_param_attr_to_list(weight_attr, 3)
        bias_attrs = _convert_param_attr_to_list(bias_attr, 3)
        self.norm1 = nn.LayerNorm(d_model)
        self.self_attn = MultiHeadAttention(d_model,nhead,dropout=attn_dropout,kdim=skdim,vdim=svdim
                                            ,weight_attr=weight_attrs[0],bias_attr=bias_attrs[0],**kwargs)
        self.dropout1 = nn.Dropout(dropout, mode="upscale_in_train")
        self.norm2 = nn.LayerNorm(d_model)
        self.cross_attn = MultiHeadAttention(d_model,nhead,dropout=attn_dropout,kdim=ckdim,vdim=cvdim,
                                             weight_attr=weight_attrs[1],bias_attr=bias_attrs[1],**kwargs)
        self.dropout2 = nn.Dropout(dropout, mode="upscale_in_train")
        self.norm3 = nn.LayerNorm(d_model)
        self.linear1 = nn.Linear(d_model, dim_feedforward, weight_attrs[2], bias_attr=bias_attrs[2])
        self.activation = eval(f'nn.{activation}()')#getattr(F, activation)
        self.dropout = nn.Dropout(act_dropout, mode="upscale_in_train")
        self.linear2 = nn.Linear(dim_feedforward, d_model, weight_attrs[2], bias_attr=bias_attrs[2])
        self.dropout3 = nn.Dropout(dropout, mode="upscale_in_train")
        self._config.pop("self")

    def forward(self, tgt, memory, tgt_mask=None, memory_mask=None, cache=None):
        residual = tgt
        if self.normalize_before:
            tgt = self.norm1(tgt)
        if cache is None:
            tgt = self.self_attn(tgt, tgt, tgt, tgt_mask, None)
        else:
            tgt, incremental_cache = self.self_attn(tgt, tgt, tgt, tgt_mask, cache[0])
        tgt = residual + self.dropout1(tgt)
        if not self.normalize_before:
            tgt = self.norm1(tgt)

        residual = tgt
        if self.normalize_before:
            tgt = self.norm2(tgt)
        if cache is None:
            tgt = self.cross_attn(tgt, memory, memory, memory_mask, None)
        else:
            tgt, static_cache = self.cross_attn(tgt, memory, memory, memory_mask, cache[1])
        tgt = residual + self.dropout2(tgt)
        if not self.normalize_before:
            tgt = self.norm2(tgt)

        residual = tgt
        if self.normalize_before:
            tgt = self.norm3(tgt)
        tgt = self.linear2(self.dropout(self.activation(self.linear1(tgt))))
        tgt = residual + self.dropout3(tgt)
        if not self.normalize_before:
            tgt = self.norm3(tgt)
        return tgt if cache is None else (tgt, (incremental_cache,static_cache))

    def gen_cache(self, memory):
        incremental_cache = self.self_attn.gen_cache(memory, type=self.self_attn.Cache)
        static_cache = self.cross_attn.gen_cache(memory, memory, type=self.cross_attn.StaticCache)
        return incremental_cache, static_cache
    
class TransformerDecoder(nn.Layer):
    def __init__(self,d_model, n_head, dim_feedforward, num_layers, **kwargs):
        super(TransformerDecoder, self).__init__()
        decoder_layer = TransformerDecoderLayer(d_model,n_head,dim_feedforward,**kwargs)
        self.layers = nn.LayerList([(decoder_layer if i == 0 else
                                  type(decoder_layer)(**decoder_layer._config))
                                 for i in range(num_layers)])
        self.num_layers = num_layers
        self.norm = nn.LayerNorm(d_model)
        self.n_head= n_head
        self.d_model =d_model

    def forward(self, tgt, memory, tgt_mask=None, memory_mask=None, cache=None):
        output = tgt 
        new_caches = []
        for i, mod in enumerate(self.layers):
            if cache is None:
                output = mod(output,
                             memory,
                             tgt_mask=tgt_mask,
                             memory_mask=memory_mask,
                             cache=None)
            else:
                output, new_cache = mod(output,
                                        memory,
                                        tgt_mask=tgt_mask,
                                        memory_mask=memory_mask,
                                        cache=cache[i])
                new_caches.append(new_cache)
                
        if self.norm is not None:
             output = self.norm(output)
            
        return output if cache is None else (logit, new_caches)

    def gen_cache(self, memory, do_zip=False):
        cache = [layer.gen_cache(memory) for layer in self.layers]
        if do_zip:
            cache = list(zip(*cache))
        return cache
    
    def _mask(self,length):
        return tensor.triu((paddle.zeros((length, length), dtype=paddle.get_default_dtype()) -float('inf')),1)
    
class Image2Text(nn.Layer):
    def __init__(self,img_encoder,txt_decoder,vocab_size,max_length,pad_id=1,eos_id=7,dropout=0):
        super(Image2Text, self).__init__()
        self.encoder = img_encoder
        self.decoder = txt_decoder
        self.vocab_size = vocab_size
        self.bos_id = pad_id
        self.eos_id = eos_id
        self.max_length = max_length
        self.word_embedding = WordEmbedding(vocab_size,self.decoder.d_model,pad_id)
        self.pos_embedding = PositionalEmbedding(self.decoder.d_model,max_length)
        self.dropout= nn.Dropout(dropout)
        self.project_out = nn.Linear(self.decoder.d_model, vocab_size)

    def forward(self, img, pre_tgt,src_mask=None,tgt_mask=None, memory_mask=None):
        with paddle.static.amp.fp16_guard():            
            memory , _ = self.encoder(img)            
            dec_input = self.dropout(self.word_embedding(pre_tgt) + \
                                     self.pos_embedding(paddle.arange(pre_tgt.shape[1]).unsqueeze(0)))
            tgt_mask= self.decoder._mask(pre_tgt.shape[1]) if tgt_mask is not None else None            
            dec_output = self.decoder(dec_input,memory,tgt_mask=tgt_mask)
            predict = self.project_out(dec_output)
        return predict
    
class FasterDecoder(Image2Text):
    def __init__(self,img_encoder,txt_decoder,vocab_size,max_length,pad_id=1,eos_id=7,dropout=0,
                 decoding_strategy="beam_search",
                 beam_size=4,
                 topk=4,
                 topp=0.0,
                 max_out_len=256,
                 diversity_rate=0.0,
                 decoding_lib=None,
                 use_fp16_decoding=False,
                 enable_faster_encoder=False,
                 use_fp16_encoder=False,
                 rel_len=False,
                 alpha=0.6):
        args = dict(locals())
        args.pop("self")
        args.pop("__class__", None)
        self.decoding_strategy = args.pop("decoding_strategy")
        self.beam_size = args.pop("beam_size")
        self.topk = args.pop("topk")
        self.topp = args.pop("topp")
        self.max_out_len = args.pop("max_out_len")
        self.diversity_rate = args.pop("diversity_rate")
        self.decoding_lib = args.pop("decoding_lib")
        self.use_fp16_decoding = args.pop("use_fp16_decoding")
        self.enable_faster_encoder = args.pop("enable_faster_encoder")
        self.use_fp16_encoder = args.pop("use_fp16_encoder")
        self.rel_len = args.pop("rel_len")
        self.alpha = args.pop("alpha")
        super(FasterDecoder, self).__init__(**args)
        
        #self.decoding_linear = nn.Linear(d_model, vocab_size)
        
        self.decoding = InferTransformerDecoding(
            decoder=self.decoder,
            word_embedding=self.word_embedding.word_embeddings,
            positional_embedding=self.pos_embedding.position_embeddings,
            linear=self.project_out,
            num_decoder_layers=self.decoder.num_layers,
            n_head=self.decoder.n_head,
            d_model=self.decoder.d_model,
            bos_id=self.bos_id,
            eos_id=self.eos_id,
            decoding_strategy=decoding_strategy,
            beam_size=beam_size,
            topk=topk,
            topp=topp,
            max_out_len=max_out_len,
            diversity_rate=self.diversity_rate,
            decoding_lib=self.decoding_lib,
            use_fp16_decoding=self.use_fp16_decoding,
            rel_len=self.rel_len,
            alpha=self.alpha)

    def forward(self, img, trg_word=None):
        enc_output,_ = self.encoder(img)
        if self.use_fp16_decoding and enc_output.dtype != paddle.float16:
            enc_output = paddle.cast(enc_output, dtype="float16")
        elif not self.use_fp16_decoding and enc_output.dtype != paddle.float32:
            enc_output = paddle.cast(enc_output, dtype="float32")
        mem_seq_lens = paddle.ones([enc_output.shape[0]],paddle.int32)*enc_output.shape[1]
        ids = self.decoding(enc_output, mem_seq_lens, trg_word=trg_word)
        return ids

deit_encoder = DistilledVisionTransformer(patch_size=16, embed_dim=384, depth=8, num_heads=6, mlp_ratio=4,)      
gpt_decoder = TransformerDecoder(d_model=384,n_head=6,dim_feedforward=1536,num_layers=6)
model=Image2Text(deit_encoder,gpt_decoder,vocab_size=3000,max_length=256)
gen = FasterDecoder(deit_encoder,gpt_decoder,vocab_size=3000,max_length=256,max_out_len=20,decoding_strategy="beam_search")
src = paddle.rand((2,3,224,224))
tgt = paddle.randint(shape=(2,20),low=1,high=3000)
model(src,tgt)
gen(src)