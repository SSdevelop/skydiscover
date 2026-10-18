// Runtime harness appended to the exact ghost-erased candidate by interface/build.py.
// All trace decoding, key allocation, database preload and thread creation are outside timing.
fn bench_u32(bytes: &[u8], offset: &mut usize) -> u32 {
    let value = u32::from_le_bytes(bytes[*offset..*offset + 4].try_into().unwrap());
    *offset += 4;
    value
}
fn main() {
    use std::sync::{Arc, Barrier};
    let args: Vec<String> = std::env::args().collect();
    let trace_path = &args[1];
    let keys: usize = args[2].parse().unwrap();
    let repetitions: usize = args[3].parse().unwrap();
    let bytes = std::fs::read(trace_path).unwrap();
    let mut offset = 0;
    let workers = bench_u32(&bytes, &mut offset) as usize;
    let mut traces = Vec::new();
    for _ in 0..workers {
        let count = bench_u32(&bytes, &mut offset) as usize;
        let mut trace = Vec::with_capacity(count);
        for _ in 0..count {
            let write = bytes[offset] != 0;
            offset += 1;
            let len = bench_u32(&bytes, &mut offset) as usize;
            let key = String::from_utf8(bytes[offset..offset + len].to_vec()).unwrap();
            offset += len;
            let value = bench_u32(&bytes, &mut offset) as i32;
            trace.push((write, key, value));
        }
        traces.push(trace);
    }
    assert_eq!(offset, bytes.len());
    let operations: usize = traces.iter().map(|v| v.len()).sum();
    for repetition in 0..=repetitions {
        let db = Arc::new(Database::new().0);
        for key in 0..keys {
            db.put(format!("{:016x}", key), (key % 2147483647) as i32);
        }
        let ready = Arc::new(Barrier::new(workers + 1));
        let start = Arc::new(Barrier::new(workers + 1));
        let mut joins = Vec::new();
        for trace in traces.clone() {
            let db = Arc::clone(&db);
            let ready = Arc::clone(&ready);
            let start = Arc::clone(&start);
            joins.push(std::thread::spawn(move || {
                ready.wait();
                start.wait();
                let mut checksum = 0i64;
                for (write, key, value) in trace {
                    if write { db.put(key, value); }
                    else { checksum = checksum.wrapping_add(std::hint::black_box(db.get(&key)).unwrap_or(0) as i64); }
                }
                checksum
            }));
        }
        ready.wait();
        let begin = std::time::Instant::now();
        start.wait();
        let mut checksum = 0i64;
        for join in joins { checksum = checksum.wrapping_add(join.join().unwrap()); }
        let elapsed = begin.elapsed().as_secs_f64();
        println!("{{\"repetition\":{},\"warmup\":{},\"operations\":{},\"seconds\":{},\"ops_per_second\":{},\"checksum\":{}}}", repetition, repetition == 0, operations, elapsed, operations as f64 / elapsed, checksum);
        std::hint::black_box(db);
    }
}
