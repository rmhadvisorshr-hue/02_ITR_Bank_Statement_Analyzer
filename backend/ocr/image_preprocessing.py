from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def _row_darkness_variance(gray_image):
    """How 'banded' a grayscale image's rows are - i.e. how sharply dark
    text rows alternate with light gaps between lines. Higher means the
    text lines are more horizontal (properly deskewed); a rotated page
    smears each line's darkness across several rows, flattening this out.
    Pure PIL/stdlib only - no numpy/OpenCV in this environment.
    """
    width, height = gray_image.size
    pixels = gray_image.load()
    row_sums = []
    step = max(1, width // 200)  # sample columns instead of walking every pixel
    for y in range(height):
        total = 0
        for x in range(0, width, step):
            total += 255 - pixels[x, y]
        row_sums.append(total)
    if not row_sums:
        return 0.0
    mean = sum(row_sums) / len(row_sums)
    return sum((v - mean) ** 2 for v in row_sums) / len(row_sums)


def detect_skew_angle(gray_image, max_angle=8.0, step=1.0):
    """Search a small angle range and return the rotation (in degrees)
    that makes the page's text rows most horizontal.

    Downsamples heavily first - this only needs to find an angle, not
    produce the final image, so it can afford to be approximate and
    still run in well under a second per page.
    """
    probe = gray_image.copy()
    probe.thumbnail((500, 700))

    best_angle = 0.0
    best_score = _row_darkness_variance(probe)

    angle = -max_angle
    while angle <= max_angle + 1e-9:
        if angle != 0:
            rotated = probe.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
            score = _row_darkness_variance(rotated)
            if score > best_score:
                best_score = score
                best_angle = angle
        angle += step

    # A real page's skew is essentially never exactly the edge of the
    # search window - landing there means the row-darkness signal was
    # still climbing when the search ran out of range (noise, a busy
    # background, a watermark), not that it found a genuine optimum.
    # Trusting it would rotate an already-level page and make its OCR
    # worse, so treat a boundary-pinned result as "no reliable skew
    # found" and leave the image alone instead.
    if abs(best_angle) >= max_angle - step / 2:
        return 0.0
    return best_angle


def deskew(gray_image, max_angle=8.0):
    """Rotate a (photographed, potentially tilted) page image level.

    Cheap early-exit: statements produced by an actual scanner are
    already level, so this only pays the rotate-and-rescan cost when the
    page is a handheld photo worth straightening.
    """
    angle = detect_skew_angle(gray_image, max_angle=max_angle)
    if abs(angle) < 0.5:
        return gray_image
    return gray_image.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)


def preprocess_image(image: Image.Image) -> Image.Image:
    """
    Preprocess image to improve OCR accuracy.
    - Convert to grayscale
    - Correct small rotations (handheld photos, not flatbed scans)
    - Enhance contrast
    - Sharpen
    """
    # Convert to grayscale
    gray_image = image.convert('L')

    # Auto-level so contrast enhancement below has real headroom on a
    # phone photo (uneven lighting, shadows) rather than just amplifying
    # whatever range the raw capture happened to use.
    gray_image = ImageOps.autocontrast(gray_image, cutoff=1)

    # Straighten before anything else - deskewing the raw grayscale image
    # keeps the geometry correct for the sharpen/contrast steps below.
    gray_image = deskew(gray_image)

    # Enhance contrast
    enhancer = ImageEnhance.Contrast(gray_image)
    high_contrast = enhancer.enhance(2.0)

    # Optional: apply a slight sharpness filter
    sharpened = high_contrast.filter(ImageFilter.SHARPEN)

    return sharpened
