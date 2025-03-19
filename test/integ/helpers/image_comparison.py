# Copyright Amazon.com Inc., or its affiliates. All Rights Reserved.

import numpy as np

import PIL.Image

from pathlib import Path


def assert_all_images_close(expected_image_directory: Path, actual_image_directory: Path):
    """
    Function to compare two images with Pillow. This is used to verify render output looks as expected.
    """

    for image in expected_image_directory.iterdir():
        if not image.is_file():
            continue

        # Pillow does not have built-in image comparison with noise tolerance,
        # so we convert them to numpy arrays to use its array comparison instead.
        actual = np.asarray(PIL.Image.open(actual_image_directory / image.name))
        expected = np.asarray(PIL.Image.open(image))

        # Two renders are rarely exactly the same, so we have a small noise tolerance for output images.
        assert np.allclose(actual, expected, atol=2)
