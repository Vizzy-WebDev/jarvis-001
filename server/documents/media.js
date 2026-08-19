// Pulls real embedded pictures out of a .docx/.pptx/.xlsx archive.
//
// An Office document doesn't store "this paragraph has an image" directly —
// it stores an r:embed id inside the content XML, and a SEPARATE _rels part
// maps that id to the actual file path (e.g. word/media/image1.png). Both
// docx.js and pptx.js gather the embed ids they encounter while walking
// text (so position is preserved — see the callers), then hand that list to
// extractImages() here to resolve to real bytes.
//
// A leaf module: imports xml.js and node:path only.

import path from 'node:path';
import { parseXml, findAll } from './xml.js';

// EMF/WMF are vector metafile formats (old Office clip-art) — no model can
// view them and Chrome/canvas can't decode them either, so they're skipped
// rather than sent as unreadable bytes. Everything else here already has a
// browser/model-safe MIME type.
const IMAGE_MIME_BY_EXTENSION = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
};

/**
 * Real dimensions read straight from each format's own header bytes — NOT a
 * file-size guess. A file-size cutoff was tried first and was a real bug: a
 * genuine, real photo can compress under 8KB just as easily as a bullet
 * icon does, and testing against an actual document (python-docx's own
 * `shp-inline-shape-access.docx` fixture) silently dropped two real,
 * legitimate 6KB/3KB pictures because of it. Pixel size is what actually
 * tells a 16x16 bullet apart from a real picture; byte size does not.
 * Returns null for a format/corrupt header this can't read — treated as "no
 * dimension known" by the caller, not as small.
 */
function imageDimensions(buf) {
  if (buf.length >= 24 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) {
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) }; // PNG: signature(8) + length(4) + "IHDR"(4) -> width/height
  }
  if (buf.length >= 10 && buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46) {
    return { width: buf.readUInt16LE(6), height: buf.readUInt16LE(8) }; // GIF: "GIF87a"/"GIF89a"(6) -> width/height, little-endian
  }
  if (buf.length >= 26 && buf[0] === 0x42 && buf[1] === 0x4d) {
    return { width: buf.readInt32LE(18), height: Math.abs(buf.readInt32LE(22)) }; // BMP: file header(14) + DIB width/height (height can be negative = top-down)
  }
  if (buf.length >= 4 && buf[0] === 0xff && buf[1] === 0xd8) {
    // JPEG has no fixed offset — dimensions live in a Start-Of-Frame marker
    // (0xFFC0-0xFFCF, excluding the DHT/DAC markers 0xC4/0xC8/0xCC) found by
    // walking the marker segments from the start of the file.
    let i = 2;
    while (i + 9 < buf.length) {
      if (buf[i] !== 0xff) {
        i++;
        continue;
      }
      const marker = buf[i + 1];
      if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
      }
      const segmentLength = buf.readUInt16BE(i + 2);
      i += 2 + segmentLength;
    }
  }
  return null;
}

/**
 * Resolves every relationship id in a part's `.rels` file to an absolute
 * path inside the archive. Targets are relative to the PART's own
 * directory, not the `_rels` folder — e.g. `word/_rels/document.xml.rels`
 * describing `media/image1.png` resolves to `word/media/image1.png`.
 */
export function resolveRelationshipTargets(files, relsPath) {
  const buf = files[relsPath];
  if (!buf) return {};
  const tree = parseXml(buf.toString('utf8'));
  const partDir = relsPath.replace(/_rels\/([^/]+)\.rels$/, '');
  const map = {};
  for (const rel of findAll(tree, 'Relationship')) {
    const target = rel.attrs.Target;
    if (!target || !rel.attrs.Id) continue;
    map[rel.attrs.Id] = target.startsWith('/') ? target.slice(1) : joinArchivePath(partDir, target);
  }
  return map;
}

function joinArchivePath(dir, relative) {
  const segments = (dir + relative).split('/');
  const out = [];
  for (const seg of segments) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') out.pop();
    else out.push(seg);
  }
  return out.join('/');
}

/**
 * Resolves a list of embed ids (in the order they were encountered in the
 * text, which is why callers gather them while walking rather than here)
 * to real image data, dropping vector formats, tiny "furniture" images
 * (icons/bullets under `minDimension` pixels on their shorter side — see
 * `imageDimensions()` for why this is a pixel check, not a byte-size one),
 * and stopping at `maxImages` so one image-heavy document can't spend a
 * whole day's quota by itself.
 */
export function extractImages(files, relsPath, embedIds, { maxImages = 4, minDimension = 32 } = {}) {
  const targets = resolveRelationshipTargets(files, relsPath);
  const images = [];
  for (const id of embedIds) {
    if (images.length >= maxImages) break;
    const targetPath = targets[id];
    const buf = targetPath && files[targetPath];
    if (!buf) continue;
    const mimeType = IMAGE_MIME_BY_EXTENSION[path.extname(targetPath).toLowerCase()];
    if (!mimeType) continue;
    const dims = imageDimensions(buf);
    // An unreadable/corrupt header (dims === null) is let through rather
    // than dropped — a format quirk this sniffer can't parse is not the
    // same thing as "this is definitely a tiny icon."
    if (dims && Math.min(dims.width, dims.height) < minDimension) continue;
    images.push({ kind: 'image', mimeType, dataBase64: buf.toString('base64') });
  }
  return images;
}
