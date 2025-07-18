import torch
import torch.nn as nn
import onnx
import os
from typing import List
from diffusers import AutoencoderKL
import traceback
import torch.nn.functional as F

# The blend functions are pure and do not need to be changed.
@torch.jit.script
def blend_v(a: torch.Tensor, b: torch.Tensor, weight: torch.Tensor) -> torch.Tensor:
    blend_extent = weight.shape[2]
    a_flipped = torch.flip(a, [2])
    a_sliced_flipped = a_flipped[:, :, :blend_extent, :]
    a_slice = torch.flip(a_sliced_flipped, [2])
    b_slice = b[:, :, :blend_extent, :]
    blended_slice = a_slice * (1 - weight) + b_slice * weight
    result = b.clone()
    result[:, :, :blend_extent, :] = blended_slice
    return result

@torch.jit.script
def blend_h(a: torch.Tensor, b: torch.Tensor, weight: torch.Tensor) -> torch.Tensor:
    blend_extent = weight.shape[3]
    a_flipped = torch.flip(a, [3])
    a_sliced_flipped = a_flipped[:, :, :, :blend_extent]
    a_slice = torch.flip(a_sliced_flipped, [3])
    b_slice = b[:, :, :, :blend_extent]
    blended_slice = a_slice * (1 - weight) + b_slice * weight
    result = b.clone()
    result[:, :, :, :blend_extent] = blended_slice
    return result

# --- The Main VAE Decoder ---
# We will JIT script an INSTANCE of this class.
class VaeDecoder(nn.Module):
    def __init__(self, traced_vae_decoder):
        super().__init__()
        self.vae_decoder = traced_vae_decoder

    def forward(self, latent: torch.Tensor) -> torch.Tensor:
        # --- Configuration ---
        tile_latent_size = 64
        vae_scale_factor = 8 
        tile_sample_size = tile_latent_size * vae_scale_factor
        tile_overlap_factor = 0.25

        overlap_size = int(tile_latent_size * (1.0 - tile_overlap_factor))
        blend_extent = int(tile_sample_size * tile_overlap_factor)
        
        # --- Pre-computation ---
        blend_extent_tensor_long = torch.tensor(blend_extent, device=latent.device, dtype=torch.long)
        y = torch.arange(blend_extent_tensor_long, device=latent.device, dtype=torch.float32).view(1, 1, -1, 1)
        weight_v = y.to(latent.dtype) / blend_extent_tensor_long.to(latent.dtype)
        x = torch.arange(blend_extent_tensor_long, device=latent.device, dtype=torch.float32).view(1, 1, 1, -1)
        weight_h = x.to(latent.dtype) / blend_extent_tensor_long.to(latent.dtype)
        
        # --- Padding ---
        latent_height, latent_width = latent.shape[2], latent.shape[3]
        pad_h = (overlap_size - (latent_height - tile_latent_size) % overlap_size) % overlap_size
        pad_w = (overlap_size - (latent_width - tile_latent_size) % overlap_size) % overlap_size
        padded_latent = F.pad(latent, (0, pad_w, 0, pad_h), mode='replicate')
        
        # --- Canvas Initialization ---
        padded_sample_h = padded_latent.shape[2] * vae_scale_factor
        padded_sample_w = padded_latent.shape[3] * vae_scale_factor
        canvas = torch.zeros(latent.shape[0], 3, padded_sample_h, padded_sample_w, dtype=latent.dtype, device=latent.device)

        # --- Tiling and Blending Loop ---
        # --- Tiling and Blending Loop ---
        # Calculate the number of steps based on overlap
        num_h_steps = (padded_latent.shape[2] - tile_latent_size) // overlap_size + 1
        num_w_steps = (padded_latent.shape[3] - tile_latent_size) // overlap_size + 1

        for i in range(num_h_steps):
            h_step = i * overlap_size
            h_start_sample = h_step * vae_scale_factor

            for j in range(num_w_steps):
                w_step = j * overlap_size
                w_start_sample = w_step * vae_scale_factor
                h_start_sample = h_step * vae_scale_factor
                w_start_sample = w_step * vae_scale_factor
                
                tile_latent = padded_latent[:, :, h_step:h_step + tile_latent_size, w_step:w_step + tile_latent_size]
                decoded_tile = self.vae_decoder(tile_latent)

                if h_step > 0:
                    existing_slice_v = canvas[:, :, h_start_sample:h_start_sample + tile_sample_size, w_start_sample:w_start_sample + tile_sample_size]
                    decoded_tile = blend_v(existing_slice_v, decoded_tile, weight_v)
                
                if w_step > 0:
                    existing_slice_h = canvas[:, :, h_start_sample:h_start_sample + tile_sample_size, w_start_sample:w_start_sample + tile_sample_size]
                    decoded_tile = blend_h(existing_slice_h, decoded_tile, weight_h)

                # --- FIX: Replace in-place assignment with `torch.where` ---
                # 1. Pad the small tile to the full canvas size
                padding = (w_start_sample, padded_sample_w - w_start_sample - tile_sample_size,
                           h_start_sample, padded_sample_h - h_start_sample - tile_sample_size)
                padded_tile = F.pad(decoded_tile, padding)
                
                # 2. Create a mask of the same size
                mask = torch.zeros_like(canvas, dtype=torch.bool)
                mask[:, :, h_start_sample:h_start_sample + tile_sample_size, w_start_sample:w_start_sample + tile_sample_size] = True
                
                # 3. Perform a full tensor reassignment
                canvas = torch.where(mask, padded_tile, canvas)

        # --- Cropping ---
        original_height_out = latent_height * vae_scale_factor
        original_width_out = latent_width * vae_scale_factor
        final_image = canvas[:, :, :original_height_out, :original_width_out]

        return final_image

def test_export(vae: AutoencoderKL):
    class VaeDecodeWrapper(nn.Module):
        def __init__(self, vae_model):
            super().__init__()
            self.vae = vae_model
        def forward(self, latents):
            return self.vae.decode(latents).sample

    tile_latent_size = 64
    dummy_latent_tile = torch.randn(1, 4, tile_latent_size, tile_latent_size, device="cuda", dtype=torch.float16)
    with torch.no_grad():
        traced_vae_decoder = torch.jit.trace(VaeDecodeWrapper(vae), dummy_latent_tile)

    # Create an instance of the VaeDecoder...
    vae_decoder_instance = VaeDecoder(traced_vae_decoder)
    # ...and then JIT script the instance.
    vae_decoder = torch.jit.script(vae_decoder_instance)
    
    latent_sample = torch.randn(1, 4, 128, 128, device="cuda", dtype=torch.float16)

    onnx_path = "onnx/vae_decoder_dynamic_loop.onnx"
    print("Exporting ONNX model with Sequence-Free loops...")
    try:
        with torch.no_grad():
            torch.onnx.export(
                vae_decoder,
                (latent_sample,),
                onnx_path,
                input_names=['latent_sample'],
                output_names=['sample'],
                dynamic_axes={
                    'latent_sample': {0: 'batch_size', 2: 'height', 3: 'width'},
                    'sample': {0: 'batch_size', 2: 'height_out', 3: 'width_out'}
                },
                opset_version=20 # opset 11+ is fine
            )
            print(f"✅ VAE Decoder exported successfully to {onnx_path}")
            print("INFO: Please inspect this model. It should contain Loop and If operators, but NO Sequence operators.")
    except Exception as e:
        print(f"❌ VAE Decoder export failed: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    os.makedirs("onnx", exist_ok=True)
    with torch.no_grad():
        print("Loading original VAE model from HuggingFace...")
        diffusers_vae = AutoencoderKL.from_pretrained(
            "madebyollin/sdxl-vae-fp16-fix", 
            torch_dtype=torch.float16
        )
        diffusers_vae.to("cuda")
        diffusers_vae.eval()
        print("✅ Original VAE model loaded.")
        test_export(diffusers_vae)