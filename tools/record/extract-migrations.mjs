// Extracts the exact SQL each migration runs, by executing the real migration
// functions against a recording stub instead of a database. Zero transcription
// risk: the SQL that lands in the Python port is the SQL Node actually runs.
import fs from 'node:fs';

const src = fs.readFileSync('server/db.js', 'utf8');
const start = src.indexOf('const MIGRATIONS = [');
const end = src.indexOf('\n];', start);
const arraySrc = src.slice(start + 'const MIGRATIONS = '.length, end + 2);

// Stubs for the two store.js functions migrations 2 and 10 reach for.
const readJson = (name, fallback) => fallback;
const writeJson = () => {};

const MIGRATIONS = eval(arraySrc);

const out = [];
for (let i = 0; i < MIGRATIONS.length; i++) {
  const execs = [];
  const prepares = [];
  const conn = {
    exec: (sql) => execs.push(sql),
    prepare: (sql) => {
      prepares.push(sql);
      return { run: () => {}, get: () => undefined, all: () => [] };
    },
  };
  let error = null;
  try {
    MIGRATIONS[i](conn);
  } catch (err) {
    error = String(err && err.message);
  }
  out.push({ version: i + 1, execs, prepares, error });
}
fs.writeFileSync('/tmp/jmig/migrations.json', JSON.stringify(out, null, 2));
console.log(`extracted ${out.length} migrations`);
for (const m of out) {
  console.log(`  v${String(m.version).padStart(2)}  exec:${m.execs.length}  prepare:${m.prepares.length}${m.error ? '  ERROR: ' + m.error : ''}`);
}
