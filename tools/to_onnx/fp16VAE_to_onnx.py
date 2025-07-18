import torch
from diffusers import AutoencoderKL
from torch.export import Dim


def main():
    """
    Exports the SDXL VAE decoder to ONNX.
    """
    model_id = "madebyollin/sdxl-vae-fp16-fix"
    output_path = "sdxl_vae_fp16_fix_decoder.onnx"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    print(f"Loading VAE from model: {model_id}")
    # We only need the VAE for this script.
    vae = AutoencoderKL.from_pretrained(
        model_id, torch_dtype=torch.float16
    )

    decoder = vae.decoder
    decoder.to(device)
    decoder.eval()
    
    print("Preparing dummy inputs for VAE decoder export...")
    batch_size = 1
    # Standard latent space size for 1024x1024 SDXL.
    latent_channels = 4
    latent_height = 128
    latent_width = 128

    latent_sample_shape = (batch_size, latent_channels, latent_height, latent_width)
    latent_sample = torch.randn(latent_sample_shape, dtype=torch.float16).to(device)

    model_args = (latent_sample,)

    print("Exporting VAE decoder to ONNX with TorchDynamo...")

    # Define dynamic axes for the model inputs.
    batch_dim = Dim("batch_size")
    height_dim = Dim("height")
    width_dim = Dim("width")
    dynamic_shapes = {
        "sample": {
            0: batch_dim,
            2: height_dim,
            3: width_dim,
        },
    }

    onnx_program = torch.onnx.export(
        decoder,
        model_args,
        input_names=["sample"],
        output_names=["output_sample"],
        dynamo=True,
        dynamic_shapes=dynamic_shapes,
        opset_version=18,
    )

    print("\n--- ONNX Model Inputs ---")
    for i, input_proto in enumerate(onnx_program.model_proto.graph.input):
        print(f"{i}: {input_proto.name}")

    print("\n--- ONNX Model Outputs ---")
    for i, output_proto in enumerate(onnx_program.model_proto.graph.output):
        print(f"{i}: {output_proto.name}")

    print(f"\nSaving ONNX model to {output_path}...")
    onnx_program.save(output_path)

    print(f"VAE decoder successfully exported to {output_path}")


if __name__ == "__main__":
    main() 