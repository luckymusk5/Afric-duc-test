// Small, dependency-free QR encoder for UTF-8 payloads up to 192 bytes.
// Version 8-L is ample for the badge enrollment and rotating-token payloads.
const SIZE = 49;
const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
let x = 1;
for (let i = 0; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
const multiply = (a, b) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);

function errorCorrection(data) {
  let generator = [1];
  for (let i = 0; i < 24; i++) {
    const next = new Array(generator.length + 1).fill(0);
    generator.forEach((v, j) => { next[j] ^= v; next[j + 1] ^= multiply(v, EXP[i]); });
    generator = next;
  }
  const remainder = [...data, ...new Array(24).fill(0)];
  for (let i = 0; i < data.length; i++) {
    const factor = remainder[i];
    if (factor) generator.forEach((v, j) => { remainder[i + j] ^= multiply(v, factor); });
  }
  return remainder.slice(data.length);
}

function encodedWords(text) {
  const bytes = [...new TextEncoder().encode(text)];
  if (bytes.length > 192) throw new Error('QR payload exceeds 192 UTF-8 bytes');
  const bits = [];
  const append = (value, length) => { for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1); };
  append(4, 4); append(bytes.length, 8); bytes.forEach(byte => append(byte, 8));
  for (let i = 0; i < Math.min(4, 1552 - bits.length); i++) bits.push(0);
  while (bits.length % 8) bits.push(0);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((v, bit) => (v << 1) | bit, 0));
  let pad = 0xec; while (data.length < 194) { data.push(pad); pad = pad === 0xec ? 0x11 : 0xec; }
  const blocks = [data.slice(0, 97), data.slice(97)];
  const checks = blocks.map(errorCorrection); const words = [];
  for (let i = 0; i < 97; i++) for (const block of blocks) words.push(block[i]);
  for (let i = 0; i < 24; i++) for (const block of checks) words.push(block[i]);
  return words;
}

function makeMatrix(text) {
  const matrix = Array.from({ length: SIZE }, () => Array(SIZE).fill(null));
  const set = (r, c, value) => { if (r >= 0 && c >= 0 && r < SIZE && c < SIZE) matrix[r][c] = Boolean(value); };
  function finder(top, left) {
    for (let dy = -1; dy <= 7; dy++) for (let dx = -1; dx <= 7; dx++) {
      const ring = dy >= 0 && dy <= 6 && dx >= 0 && dx <= 6 && (dy === 0 || dy === 6 || dx === 0 || dx === 6 || (dy >= 2 && dy <= 4 && dx >= 2 && dx <= 4));
      set(top + dy, left + dx, ring);
    }
  }
  finder(0, 0); finder(0, SIZE - 7); finder(SIZE - 7, 0);
  for (let i = 8; i < SIZE - 8; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
  const alignment = [6, 24, 42];
  for (const row of alignment) for (const col of alignment) {
    if (matrix[row][col] !== null) continue;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(row + dy, col + dx, Math.max(Math.abs(dy), Math.abs(dx)) !== 1);
  }
  // Version 8 BCH information.
  let version = 8 << 12;
  for (let i = 17; i >= 12; i--) if ((version >>> i) & 1) version ^= 0x1f25 << (i - 12);
  version |= 8 << 12;
  for (let i = 0; i < 18; i++) {
    const bit = ((version >>> i) & 1) !== 0; const a = SIZE - 11 + i % 3; const b = Math.floor(i / 3);
    set(b, a, bit); set(a, b, bit);
  }
  // Format information: error-correction level L and mask pattern 0.
  let format = 8 << 10;
  for (let i = 14; i >= 10; i--) if ((format >>> i) & 1) format ^= 0x537 << (i - 10);
  format = ((8 << 10) | (format & 0x3ff)) ^ 0x5412;
  for (let i = 0; i <= 5; i++) set(i, 8, (format >>> i) & 1);
  set(7, 8, (format >>> 6) & 1); set(8, 8, (format >>> 7) & 1); set(8, 7, (format >>> 8) & 1);
  for (let i = 9; i < 15; i++) set(8, 14 - i, (format >>> i) & 1);
  for (let i = 0; i < 8; i++) set(8, SIZE - 1 - i, (format >>> i) & 1);
  for (let i = 8; i < 15; i++) set(SIZE - 15 + i, 8, (format >>> i) & 1);
  set(SIZE - 8, 8, true);

  const bits = encodedWords(text).flatMap(word => Array.from({ length: 8 }, (_, i) => (word >>> (7 - i)) & 1));
  let index = 0; let upward = true;
  for (let right = SIZE - 1; right >= 1; right -= 2) {
    if (right === 6) right--;
    for (let step = 0; step < SIZE; step++) {
      const row = upward ? SIZE - 1 - step : step;
      for (let side = 0; side < 2; side++) {
        const col = right - side;
        if (matrix[row][col] !== null) continue;
        const bit = index < bits.length ? bits[index++] : 0;
        set(row, col, Boolean(bit ^ ((row + col) % 2 === 0 ? 1 : 0)));
      }
    }
    upward = !upward;
  }
  return matrix;
}

export function QrSvg({ value, size = 240 }) {
  const matrix = makeMatrix(value); const quiet = 4; const dimension = SIZE + quiet * 2;
  const path = [];
  matrix.forEach((row, y) => row.forEach((dark, x) => { if (dark) path.push(`M${x + quiet},${y + quiet}h1v1h-1z`); }));
  return <svg xmlns="http://www.w3.org/2000/svg" viewBox={`0 0 ${dimension} ${dimension}`} width={size} height={size} shapeRendering="crispEdges" role="img" aria-label="QR code"><rect width="100%" height="100%" fill="#fff"/><path d={path.join('')} fill="#000"/></svg>;
}
