use genvm_modules::llm::prompt;

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn encode(img: image::DynamicImage, format: image::ImageFormat) -> Vec<u8> {
    let mut out = std::io::Cursor::new(Vec::new());
    img.write_to(&mut out, format).unwrap();
    out.into_inner()
}

fn rgb() -> image::DynamicImage {
    image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(
        64,
        64,
        image::Rgb([200, 30, 30]),
    ))
}

/// Encoded row by row, so the test never holds the bitmap either
fn black_rgba16_png(side: u32) -> Vec<u8> {
    let mut data = Vec::new();
    let mut encoder = png::Encoder::new(&mut data, side, side);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Sixteen);
    encoder.set_compression(png::Compression::Fastest);
    let mut writer = encoder.write_header().unwrap();
    let mut stream = writer.stream_writer().unwrap();
    let row = vec![0u8; side as usize * 8];
    for _ in 0..side {
        std::io::Write::write_all(&mut stream, &row).unwrap();
    }
    stream.finish().unwrap();
    writer.finish().unwrap();
    data
}

fn accepts(data: &[u8]) -> bool {
    let kind = prompt::ImageType::sniff(data).expect("test data must pass the header sniff");
    kind.decodes(data)
}

// -- Well-formed images --

#[test]
fn accepts_png() {
    assert!(accepts(&encode(rgb(), image::ImageFormat::Png)));
}

#[test]
fn accepts_jpeg() {
    assert!(accepts(&encode(rgb(), image::ImageFormat::Jpeg)));
}

#[test]
fn accepts_progressive_jpeg() {
    assert!(accepts(include_bytes!("data/progressive.jpg")));
}

#[test]
fn accepts_grey_jpeg() {
    let img = image::DynamicImage::ImageLuma8(image::GrayImage::new(16, 16));
    assert!(accepts(&encode(img, image::ImageFormat::Jpeg)));
}

#[test]
fn accepts_16_bit_rgba_png() {
    let img = image::DynamicImage::ImageRgba16(image::ImageBuffer::new(16, 16));
    assert!(accepts(&encode(img, image::ImageFormat::Png)));
}

#[test]
fn accepts_png_with_trailing_junk() {
    let mut data = encode(rgb(), image::ImageFormat::Png);
    data.extend_from_slice(&[0; 1000]);
    assert!(accepts(&data));
}

// -- Header passes, body does not --

#[test]
fn rejects_png_magic_with_garbage() {
    let mut data = b"\x89PNG\r\n\x1a\n".to_vec();
    data.extend(b"garbage".repeat(50));
    assert!(!accepts(&data));
}

#[test]
fn rejects_bare_png_magic() {
    assert!(!accepts(b"\x89PNG\r\n\x1a\n"));
}

#[test]
fn rejects_jfif_magic_with_garbage() {
    let mut data = b"\xff\xd8\xff\xe0".to_vec();
    data.extend(b"garbage".repeat(50));
    assert!(!accepts(&data));
}

#[test]
fn rejects_truncated_png() {
    let data = encode(rgb(), image::ImageFormat::Png);
    assert!(!accepts(&data[..data.len() / 2]));
}

#[test]
fn rejects_truncated_jpeg() {
    let data = encode(rgb(), image::ImageFormat::Jpeg);
    assert!(!accepts(&data[..data.len() / 2]));
}

#[test]
fn rejects_jpeg_magic_over_png_body() {
    let mut data = b"\xff\xd8\xff\xe0".to_vec();
    data.extend(encode(rgb(), image::ImageFormat::Png));
    assert!(!accepts(&data));
}

#[test]
fn rejects_png_magic_over_jpeg_body() {
    let mut data = b"\x89PNG\r\n\x1a\n".to_vec();
    data.extend(encode(rgb(), image::ImageFormat::Jpeg));
    assert!(!accepts(&data));
}

// -- Malformed PNG structure --

#[test]
fn rejects_palette_png_without_plte() {
    assert!(!accepts(&hex("89504e470d0a1a0a0000000d49484452000000040000000408030000009e2f6e4c0000000e4944415478da636004020654020000b40011197e69d80000000049454e44ae426082")));
}

#[test]
fn rejects_palette_png_with_16_bit_depth() {
    assert!(!accepts(&hex("89504e470d0a1a0a0000000d4948445200000004000000041003000000cebfb20f00000003504c5445000000a77a3dda0000000b4944415478da6360200c000024000125c2a8e30000000049454e44ae426082")));
}

#[test]
fn rejects_png_with_invalid_color_type() {
    assert!(!accepts(&hex("89504e470d0a1a0a0000000d4948445200000004000000040805000000bb4431900000000b4944415478da6360c0040000140001ee5a69090000000049454e44ae426082")));
}

#[test]
fn rejects_png_with_zero_dimensions() {
    assert!(!accepts(&hex("89504e470d0a1a0a0000000d4948445200000000000000000802000000b4e9eb45000000084944415478da0300000000016fddc9910000000049454e44ae426082")));
}

#[test]
fn rejects_png_with_bad_header_crc() {
    assert!(!accepts(&hex("89504e470d0a1a0a0000000d4948445200000004000000040802000000000000000000000e4944415478da636840020cc47100705218019eaebe6f0000000049454e44ae426082")));
}

// -- Dimension and allocation limits --

#[test]
fn rejects_png_declaring_huge_dimensions() {
    assert!(!accepts(&hex("89504e470d0a1a0a0000000d494844527fffffff7fffffff08020000009bab9c310000000b4944415478da636040050000100001aa19f8820000000049454e44ae426082")));
}

#[test]
fn rejects_png_wider_than_the_limit() {
    let img = image::DynamicImage::ImageLuma8(image::GrayImage::new(prompt::IMAGE_MAX_SIDE + 1, 1));
    assert!(!accepts(&encode(img, image::ImageFormat::Png)));
}

#[test]
fn rejects_png_taller_than_the_limit() {
    let img = image::DynamicImage::ImageLuma8(image::GrayImage::new(1, prompt::IMAGE_MAX_SIDE + 1));
    assert!(!accepts(&encode(img, image::ImageFormat::Png)));
}

#[test]
fn rejects_jpeg_wider_than_the_limit() {
    let img = image::DynamicImage::ImageLuma8(image::GrayImage::new(prompt::IMAGE_MAX_SIDE + 1, 8));
    assert!(!accepts(&encode(img, image::ImageFormat::Jpeg)));
}

#[test]
fn accepts_16_bit_rgba_png_at_the_side_limit() {
    // 512 MB decoded, which a whole-bitmap decode under the JPEG budget would refuse
    assert!(accepts(&black_rgba16_png(prompt::IMAGE_MAX_SIDE)));
}

#[test]
fn accepts_png_at_the_side_limit() {
    let img = image::DynamicImage::ImageLuma8(image::GrayImage::new(prompt::IMAGE_MAX_SIDE, 1));
    assert!(accepts(&encode(img, image::ImageFormat::Png)));
}
