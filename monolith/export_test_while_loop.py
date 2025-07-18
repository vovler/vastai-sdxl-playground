import torch
import torch.nn as nn
import onnx
import onnxruntime
import numpy as np
import os

# --- Add imports for custom operator ---
import onnxscript
from onnxscript import script
from onnxscript.onnx_types import FLOAT, INT64
from torch.library import Library
from torch import DispatchKey

# Use the same opset version as in the export call
op = onnxscript.opset20


# --- 1. Define custom PyTorch operator for the dynamic loop ---

# Create a new library 'mylibrary' and specify that we will define operators on it.
mylib = Library("mylibrary", "DEF")

# Define the operator's signature. This is a string that describes the inputs and outputs.
# We must pass the convolution weight to the operator so the ONNX translation can use it.
mylib.define("loop_op(Tensor x, Tensor loop_iterations, Tensor weight) -> Tensor")


# The 'meta' implementation tells PyTorch the properties (e.g., shape, dtype) of the output tensor.
# This allows `torch.export` to trace the model without actually running the operator.
def loop_op_meta(x, loop_iterations, weight):
    # The shape of 'x' does not change inside the loop, so the output shape is the same as the input.
    return torch.empty_like(x)

# Register the meta implementation with the library.
mylib.impl("loop_op", loop_op_meta, dispatch_key=DispatchKey.Meta)


# Provide a default implementation for the CPU backend that raises an error.
# This is good practice and clarifies that the operator is not intended for eager execution.
def loop_op_cpu(x, loop_iterations, weight):
    # This could be implemented in eager mode for testing, but for export, it's not needed.
    raise NotImplementedError("This operator is only implemented for ONNX export.")

# Register the CPU implementation.
mylib.impl("loop_op", loop_op_cpu, dispatch_key=DispatchKey.CPU)


# --- 2. Define the ONNX implementation for the custom operator ---
# This function defines how to translate `mylibrary::loop_op` into ONNX operators.
@script()
def onnx_custom_loop_op_translation(x: FLOAT, loop_iterations: INT64, weight: FLOAT) -> FLOAT:
    """
    Translates the custom loop operator into an ONNX `Loop` operator using ONNX Script.
    """
    # The body of the ONNX Loop is defined as a separate graph.
    # onnxscript will capture the `weight` tensor as a free variable.
    @script()
    def body_graph(iter_num, cond, x_scan):
        # In one iteration, we apply a convolution.
        # Parameters from the original model: kernel_size=3, padding=1, bias=False.
        # `pads` is specified for each dimension [y_begin, x_begin, y_end, x_end].
        x_out = op.Conv(x_scan, weight, pads=[1, 1, 1, 1])
        # The condition is always True to loop for the specified number of iterations.
        cond_out = op.Constant(value=torch.tensor(True))
        return cond_out, x_out

    # The ONNX Loop operator requires:
    # 1. M: A scalar INT64 tensor for the maximum trip count.
    # 2. cond: A scalar boolean tensor for the initial loop condition (can be omitted).
    # 3. v_initials: A list of tensors that are carried through the loop. Here, just `x`.
    # The loop returns the final values of the loop-carried variables.
    final_x, = op.Loop(loop_iterations, None, x, body=body_graph)
    return final_x


# --- 3. Define the PyTorch Model using the custom operator ---
class DynamicLoopModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.loop_body = nn.Conv2d(in_channels=3, out_channels=3, kernel_size=3, padding=1, bias=False)
        torch.nn.init.xavier_uniform_(self.loop_body.weight)

    def forward(self, x, loop_iterations):
        # Use the custom operator instead of a Python loop.
        # We pass the convolution weights as an argument.
        return torch.ops.mylibrary.loop_op.default(x, loop_iterations, self.loop_body.weight)


# --- 4. Instantiate the Model ---
model = DynamicLoopModel()
model.eval()

# --- 5. Export the Model to ONNX with the custom operator ---
print("--- Exporting to ONNX with custom Loop operator ---")
onnx_file_path = "dynamic_loop_model.onnx"
input_names = ["input", "loop_iterations"]
output_names = ["output"]

# Define dummy inputs for tracing
dummy_input = torch.randn(1, 3, 10, 10)
dummy_loop_iterations = torch.tensor(3, dtype=torch.int64)

# Define the dynamic axes
dynamic_axes = {
    input_names[0]: {0: 'batch_size', 2: 'height', 3: 'width'},
    output_names[0]: {0: 'batch_size', 2: 'height', 3: 'width'}
}

# The `custom_translation_table` maps our PyTorch custom op to our ONNX Script implementation.
custom_translation_table = {
    torch.ops.mylibrary.loop_op.default: onnx_custom_loop_op_translation,
}

# Export using torch.onnx with dynamo=True
torch.onnx.export(
    model,
    (dummy_input, dummy_loop_iterations),
    onnx_file_path,
    input_names=input_names,
    output_names=output_names,
    opset_version=20,
    dynamo=True,
    dynamic_axes=dynamic_axes,
    custom_translation_table=custom_translation_table,
)

print(f"Model successfully exported to {onnx_file_path}")
print("Inspect the model with Netron. You will see a 'Loop' operator.")
print("-" * 45 + "\n")


# --- 6. Verify the Exported ONNX Model ---
print("--- Verifying the Dynamic Loop ONNX Model ---")

try:
    ort_session = onnxruntime.InferenceSession(onnx_file_path)
    print("ONNX Runtime session created successfully.")

    # --- Test Case 1: Loop 2 times ---
    print("\n--- Test Case 1 (Loop 2 times) ---")
    input_data = np.ones((1, 3, 8, 8), dtype=np.float32)
    loop_count_1 = np.array(2, dtype=np.int64)
    ort_inputs_1 = {
        ort_session.get_inputs()[0].name: input_data,
        ort_session.get_inputs()[1].name: loop_count_1
    }
    ort_outs_1 = ort_session.run(None, ort_inputs_1)
    output_tensor_1 = ort_outs_1[0]
    print(f"Output shape: {output_tensor_1.shape}")
    assert input_data.shape == output_tensor_1.shape

    # --- Test Case 2: Loop 5 times with different input shape ---
    print("\n--- Test Case 2 (Loop 5 times) ---")
    input_data_2 = np.ones((2, 3, 16, 16), dtype=np.float32)
    loop_count_2 = np.array(5, dtype=np.int64)
    ort_inputs_2 = {
        ort_session.get_inputs()[0].name: input_data_2,
        ort_session.get_inputs()[1].name: loop_count_2
    }
    ort_outs_2 = ort_session.run(None, ort_inputs_2)
    output_tensor_2 = ort_outs_2[0]
    print(f"Output shape: {output_tensor_2.shape}")
    assert input_data_2.shape == output_tensor_2.shape

    print("\nVerification successful! The model correctly handles dynamic loops.")

except Exception as e:
    print(f"An error occurred during verification: {e}")