//! Min, mean and max temperature per station, from `<station>;<temperature>` lines.
//!
//! The file is mapped into memory and scanned eight bytes at a time for the `;`, hashing the name as it goes. The
//! temperature is parsed as an integer of tenths without branches, and added to an open-addressing table whose keys
//! point into the mapped file. The table is sorted by name and printed as `name=min/mean/max`, one line each.

use std::env;
use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::os::unix::io::AsRawFd;
use std::slice;

unsafe extern "C" {
    fn mmap(addr: *mut u8, len: usize, prot: i32, flags: i32, fd: i32, offset: i64) -> *mut u8;
    fn madvise(addr: *mut u8, len: usize, advice: i32) -> i32;
}

const PROT_READ: i32 = 1;
const MAP_PRIVATE: i32 = 2;
const MADV_SEQUENTIAL: i32 = 2;
const MAP_FAILED: *mut u8 = !0usize as *mut u8;

const SLOTS: usize = 1 << 17;
/// A line is at most 100 bytes of name, `;`, `-99.9` and `\n`: the fast path needs this many bytes after its start.
const TAIL: usize = 128;
const SEMIS: u64 = 0x3B3B_3B3B_3B3B_3B3B;
const ONES: u64 = 0x0101_0101_0101_0101;
const HIGHS: u64 = 0x8080_8080_8080_8080;

#[derive(Clone, Copy)]
struct Slot {
    start: u32,
    len: u32,
    hash: u64,
    min: i32,
    max: i32,
    count: u64,
    sum: i64,
}

const EMPTY: Slot = Slot { start: 0, len: 0, hash: 0, min: i32::MAX, max: i32::MIN, count: 0, sum: 0 };

struct Table {
    slots: Vec<Slot>,
}

impl Table {
    fn new() -> Self {
        Table { slots: vec![EMPTY; SLOTS] }
    }

    #[inline(always)]
    fn add(&mut self, data: &[u8], start: usize, len: usize, hash: u64, value: i32) {
        let mut i = (hash ^ (hash >> 29)) as usize & (SLOTS - 1);
        loop {
            let slot = unsafe { self.slots.get_unchecked_mut(i) };
            if slot.len == 0 {
                *slot = Slot { start: start as u32, len: len as u32, hash, min: value, max: value, count: 1, sum: value as i64 };
                return;
            }
            if slot.hash == hash
                && slot.len as usize == len
                && data[slot.start as usize..slot.start as usize + len] == data[start..start + len]
            {
                slot.min = slot.min.min(value);
                slot.max = slot.max.max(value);
                slot.count += 1;
                slot.sum += value as i64;
                return;
            }
            i = (i + 1) & (SLOTS - 1);
        }
    }
}

#[inline(always)]
fn word(data: &[u8], at: usize) -> u64 {
    unsafe { (data.as_ptr().add(at) as *const u64).read_unaligned() }.to_le()
}

#[inline(always)]
fn semicolons(w: u64) -> u64 {
    let x = w ^ SEMIS;
    x.wrapping_sub(ONES) & !x & HIGHS
}

#[inline(always)]
fn mix(h: u64, w: u64) -> u64 {
    (h.rotate_left(5) ^ w).wrapping_mul(0x517c_c1b7_2722_0a95)
}

/// The temperature that starts at `at`, in tenths, and the bytes it takes with its newline.
#[inline(always)]
fn temperature(data: &[u8], at: usize) -> (i32, usize) {
    let w = word(data, at);
    let negated = !w;
    let dot = (negated & 0x1010_1000).trailing_zeros();
    let signed = ((negated << 59) as i64) >> 63;
    let keep = !((signed as u64) & 0xFF);
    let digits = ((w & keep) << (28 - dot)) & 0x0F_000F_0F00;
    let abs = ((digits.wrapping_mul(0x640a_0001)) >> 32) & 0x3FF;
    let value = ((abs as i64) ^ signed) - signed;
    (value as i32, (dot as usize >> 3) + 3)
}

/// The value of a temperature written plainly, for the last lines of the file.
fn slow_temperature(text: &[u8]) -> i32 {
    let (sign, digits) = if text[0] == b'-' { (-1, &text[1..]) } else { (1, text) };
    let mut v = 0i32;
    for &b in digits {
        if b != b'.' {
            v = v * 10 + (b - b'0') as i32;
        }
    }
    sign * v
}

fn scan(data: &[u8], mut pos: usize, end: usize, table: &mut Table) {
    let fast_end = end.min(data.len().saturating_sub(TAIL));
    while pos < fast_end {
        let start = pos;
        let mut h = 0u64;
        loop {
            let w = word(data, pos);
            let m = semicolons(w);
            if m != 0 {
                let j = m.trailing_zeros() as usize >> 3;
                if j > 0 {
                    h = mix(h, w & ((1u64 << (8 * j)) - 1));
                }
                pos += j;
                break;
            }
            h = mix(h, w);
            pos += 8;
        }
        let len = pos - start;
        let (value, used) = temperature(data, pos + 1);
        table.add(data, start, len, h ^ len as u64, value);
        pos += 1 + used;
    }
    while pos < end {
        let start = pos;
        let semi = start + data[start..end].iter().position(|&b| b == b';').expect("a line without ;");
        let nl = semi + data[semi..end].iter().position(|&b| b == b'\n').unwrap_or(end - semi);
        let value = slow_temperature(&data[semi + 1..nl]);
        let mut h = 0u64;
        let mut at = start;
        while at + 8 <= semi {
            h = mix(h, word_slow(data, at, 8));
            at += 8;
        }
        if at < semi {
            h = mix(h, word_slow(data, at, semi - at));
        }
        let len = semi - start;
        table.add(data, start, len, h ^ len as u64, value);
        pos = nl + 1;
    }
}

/// The bytes at `at`, up to 8 of them, as the fast path reads them, without reading past them.
fn word_slow(data: &[u8], at: usize, n: usize) -> u64 {
    let mut bytes = [0u8; 8];
    bytes[..n].copy_from_slice(&data[at..at + n]);
    u64::from_le_bytes(bytes)
}

fn write_tenths(out: &mut impl Write, t: i64) -> io::Result<()> {
    let sign = if t < 0 { "-" } else { "" };
    write!(out, "{sign}{}.{}", t.abs() / 10, t.abs() % 10)
}

fn main() -> io::Result<()> {
    let path = env::args().nth(1).expect("usage: brc <measurements file>");
    let file = File::open(path)?;
    let len = file.metadata()?.len() as usize;
    if len == 0 {
        return Ok(());
    }
    let data: &'static [u8] = unsafe {
        let p = mmap(std::ptr::null_mut(), len, PROT_READ, MAP_PRIVATE, file.as_raw_fd(), 0);
        assert!(p != MAP_FAILED, "mmap failed");
        madvise(p, len, MADV_SEQUENTIAL);
        slice::from_raw_parts(p, len)
    };
    let mut table = Table::new();
    scan(data, 0, data.len(), &mut table);
    let mut rows: Vec<&Slot> = table.slots.iter().filter(|s| s.len > 0).collect();
    rows.sort_unstable_by(|a, b| {
        data[a.start as usize..(a.start + a.len) as usize].cmp(&data[b.start as usize..(b.start + b.len) as usize])
    });
    let mut out = BufWriter::with_capacity(1 << 20, io::stdout().lock());
    for s in rows {
        out.write_all(&data[s.start as usize..(s.start + s.len) as usize])?;
        out.write_all(b"=")?;
        write_tenths(&mut out, s.min as i64)?;
        out.write_all(b"/")?;
        let c = s.count as i64;
        write_tenths(&mut out, (2 * s.sum + c).div_euclid(2 * c))?;
        out.write_all(b"/")?;
        write_tenths(&mut out, s.max as i64)?;
        out.write_all(b"\n")?;
    }
    out.flush()
}
