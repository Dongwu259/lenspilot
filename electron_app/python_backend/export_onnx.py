"""
export_onnx.py — Convert PyTorch model to ONNX + quantize to INT8
Usage: python export_onnx.py [--model <checkpoint_path>] [--output <onnx_path>]
"""

import os
import sys
import argparse
import torch
import torch.nn as nn
import torchvision.models as models
import numpy as np

# Add current dir for imports
sys.path.insert(0, os.path.dirname(__file__))

class ResNet18ForExport(nn.Module):
    """Standalone ResNet-18 for ONNX export (needs nn.Module for torch.onnx).

    forward returns BOTH the final score and the layer4 activation map, so the
    exported ONNX exposes a second output used by Score-CAM explainability
    (gradient-free CAM on CPU/ONNX). Consumers that only need the score simply
    read output[0].
    """
    def __init__(self, dropout=0.3):
        super().__init__()
        self.backbone = models.resnet18(weights=None)
        out = self.backbone.fc.in_features
        self.backbone.fc = nn.Sequential(
            nn.Dropout(dropout), nn.Linear(out, 256), nn.ReLU(inplace=True),
            nn.Dropout(dropout * 0.5), nn.Linear(256, 1),
        )

    def forward(self, x):
        # Mirror ResNet's forward to capture the layer4 activation map.
        b = self.backbone
        x = b.conv1(x)
        x = b.bn1(x)
        x = b.relu(x)
        x = b.maxpool(x)
        x = b.layer1(x)
        x = b.layer2(x)
        x = b.layer3(x)
        layer4 = b.layer4(x)          # (1, 512, 7, 7) for 224×224 input
        pooled = b.avgpool(layer4)
        pooled = torch.flatten(pooled, 1)
        score = b.fc(pooled)
        return score, layer4


def export(checkpoint_path, output_path, img_size=224):
    """Export PyTorch model to ONNX format."""
    print(f"Loading checkpoint: {checkpoint_path}")

    # Load checkpoint
    checkpoint = torch.load(checkpoint_path, map_location='cpu', weights_only=False)
    state_dict = checkpoint.get('model_state_dict', checkpoint)
    config = checkpoint.get('config', {})

    dropout = config.get('model', {}).get('dropout', 0.3)
    img_size = config.get('data', {}).get('img_size', 224)

    # Create model (standalone nn.Module for export)
    model = ResNet18ForExport(dropout=dropout)

    # Handle state dict keys (checkpoint may or may not have 'backbone.' prefix)
    if not any(k.startswith('backbone.') for k in state_dict.keys()):
        state_dict = {f'backbone.{k}': v for k, v in state_dict.items()}

    model.load_state_dict(state_dict)
    model.eval()

    # Create dummy input
    dummy = torch.randn(1, 3, img_size, img_size)

    # Export to ONNX — outputs both `score` and `layer4` (for Score-CAM).
    print(f"Exporting ONNX (input: {img_size}x{img_size}, outputs: score + layer4)...")
    torch.onnx.export(
        model,
        dummy,
        output_path,
        export_params=True,
        opset_version=17,
        input_names=['input'],
        output_names=['score', 'layer4'],
    )

    # Verify
    import onnx
    onnx_model = onnx.load(output_path)
    onnx.checker.check_model(onnx_model)
    print(f"ONNX model verified OK")

    # Get file sizes
    pth_mb = os.path.getsize(checkpoint_path) / 1024 / 1024
    onnx_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"PyTorch: {pth_mb:.1f} MB → ONNX: {onnx_mb:.1f} MB (saved to {output_path})")
    print("Done!")


def main():
    parser = argparse.ArgumentParser(description='Export ResNet-18 scorer to ONNX')
    parser.add_argument('--model', type=str, default=None, help='Checkpoint path')
    parser.add_argument('--output', type=str, default=None, help='Output ONNX path')
    args = parser.parse_args()

    if args.model:
        checkpoint_path = args.model
    else:
        from model_loader import find_model_file
        checkpoint_path = find_model_file('best_model.pth')
        if not checkpoint_path:
            print("Error: best_model.pth not found. Use --model to specify path.")
            sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        model_dir = os.path.dirname(checkpoint_path) or '.'
        output_path = os.path.join(model_dir, 'best_model.onnx')

    export(checkpoint_path, output_path)


if __name__ == '__main__':
    main()
