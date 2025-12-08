# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from pathlib import Path

import numpy as np
import PIL.Image


def assert_all_images_close(expected_image_directory: Path, actual_image_directory: Path):
    """
    Function to compare two images with Pillow. This is used to verify render output looks as expected.
    """

    for image in expected_image_directory.iterdir():
        if not image.is_file() or image.name == ".DS_Store":
            continue

        # Pillow does not have built-in image comparison with noise tolerance,
        # so we convert them to numpy arrays to use its array comparison instead.
        try:
            actual = np.asarray(PIL.Image.open(actual_image_directory / image.name))
        except FileNotFoundError:
            actual_images_per_line = "\n".join(
                sorted(p.name for p in actual_image_directory.iterdir())
            )
            raise AssertionError(
                f"Image {image.name} not found in {actual_image_directory}. Contents:\n{actual_images_per_line}"
            ) from None
        expected = np.asarray(PIL.Image.open(image))

        # Check that the two images are the same within a tolerance.
        # It's normal for there to be noise in an output image, so it is unlikely that two
        # renders will be exactly the same.
        # Check both: max difference and percentage of pixels outside tolerance
        diff = np.abs(actual.astype(int) - expected.astype(int))
        max_diff = diff.max()
        pixels_outside_tolerance = np.sum(diff > 2)
        total_pixels = diff.size
        percent_outside = (pixels_outside_tolerance / total_pixels) * 100

        # Pass if max diff <= 2 OR less than 0.5% of pixels are outside tolerance
        if max_diff > 2 and percent_outside > 0.5:
            assert (
                False
            ), f"Image {image.name} is not close to the expected image (max diff: {max_diff}, {percent_outside:.2f}% pixels outside tolerance)"
