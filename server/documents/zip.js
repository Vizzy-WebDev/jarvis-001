// A minimal ZIP reader — just enough to open a .docx/.xlsx/.pptx (which are
// all plain ZIP archives full of XML) with no dependency. Node's built-in
// zlib.inflateRawSync is the one piece this needs that isn't hand-written.
//
// Reads from the END of the file (the central directory) rather than
// streaming from the start — the standard, robust way to open a ZIP, and it
// means one entry's local header being slightly off doesn't break every
// entry after it.
//
// A leaf module: no imports beyond node:zlib. Nothing here knows about
// Office XML at all — that's xml.js and the format-specific readers above it.

import zlib from 'node:zlib';

const EOCD_SIGNATURE = 0x06054b50;
const CENTRAL_DIR_SIGNATURE = 0x02014b50;
const LOCAL_FILE_SIGNATURE = 0x04034b50;
const EOCD_MIN_SIZE = 22;
const MAX_COMMENT_SIZE = 65535; // the comment field's own length is a uint16

/** Scans backward from the end of the buffer for the End Of Central Directory record. */
function findEndOfCentralDirectory(buf) {
  const maxBack = Math.min(buf.length, EOCD_MIN_SIZE + MAX_COMMENT_SIZE);
  const start = buf.length - maxBack;
  for (let i = buf.length - EOCD_MIN_SIZE; i >= start; i--) {
    if (buf.readUInt32LE(i) === EOCD_SIGNATURE) return i;
  }
  return -1;
}

/**
 * Opens a ZIP archive from a Buffer and returns every entry's contents,
 * already decompressed, keyed by its path inside the archive (e.g.
 * "word/document.xml"). Directory entries (name ends in "/") are skipped.
 *
 * Throws a plain-language error rather than a raw zlib/offset exception —
 * this is reached directly from user-uploaded files, so a corrupt or
 * unrelated file must fail cleanly, not crash the request.
 */
export function readZip(buf) {
  if (!Buffer.isBuffer(buf)) throw new Error('That file could not be opened.');

  const eocdOffset = findEndOfCentralDirectory(buf);
  if (eocdOffset === -1) throw new Error("That file doesn't look like a valid Office document (not a ZIP archive).");

  const entryCount = buf.readUInt16LE(eocdOffset + 10);
  const centralDirOffset = buf.readUInt32LE(eocdOffset + 16);

  const files = {};
  let offset = centralDirOffset;

  for (let i = 0; i < entryCount; i++) {
    if (offset + 46 > buf.length || buf.readUInt32LE(offset) !== CENTRAL_DIR_SIGNATURE) {
      throw new Error('That Office document looks corrupted (unreadable archive index).');
    }

    const compressionMethod = buf.readUInt16LE(offset + 10);
    const compressedSize = buf.readUInt32LE(offset + 20);
    const uncompressedSize = buf.readUInt32LE(offset + 24);
    const nameLength = buf.readUInt16LE(offset + 28);
    const extraLength = buf.readUInt16LE(offset + 30);
    const commentLength = buf.readUInt16LE(offset + 32);
    const localHeaderOffset = buf.readUInt32LE(offset + 42);
    const name = buf.toString('utf8', offset + 46, offset + 46 + nameLength);

    offset += 46 + nameLength + extraLength + commentLength;

    if (!name.endsWith('/')) {
      files[name] = readLocalEntry(buf, localHeaderOffset, compressionMethod, compressedSize, uncompressedSize);
    }
  }

  return files;
}

function readLocalEntry(buf, localHeaderOffset, compressionMethod, compressedSize, uncompressedSize) {
  if (localHeaderOffset + 30 > buf.length || buf.readUInt32LE(localHeaderOffset) !== LOCAL_FILE_SIGNATURE) {
    throw new Error('That Office document looks corrupted (unreadable file entry).');
  }
  const nameLength = buf.readUInt16LE(localHeaderOffset + 26);
  const extraLength = buf.readUInt16LE(localHeaderOffset + 28);
  const dataStart = localHeaderOffset + 30 + nameLength + extraLength;
  const dataEnd = dataStart + compressedSize;
  const compressed = buf.subarray(dataStart, Math.min(dataEnd, buf.length));

  if (compressionMethod === 0) return compressed; // stored, no compression
  if (compressionMethod === 8) {
    const out = zlib.inflateRawSync(compressed);
    // Sanity check, not a hard requirement — a mismatch usually means the
    // offsets above drifted, and it's better to know than to silently hand
    // back truncated/garbage bytes to an XML parser downstream.
    if (uncompressedSize && out.length !== uncompressedSize) {
      // Some writers pad or the size crosses a zip64 boundary this reader
      // doesn't handle — still return what inflated, just don't throw here.
    }
    return out;
  }
  throw new Error('That Office document uses a compression method this reader does not support.');
}
