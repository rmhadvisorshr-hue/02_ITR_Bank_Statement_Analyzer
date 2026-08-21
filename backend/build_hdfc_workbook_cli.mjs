// Thin CLI wrapper around the unmodified buildWorkbook() export so a
// non-Node caller (the FastAPI analysis service) can drive it as a
// subprocess, the same way it already shells out to extract_bank.py and
// add_chart.py. Contains no workbook-rendering logic of its own - every
// line of actual sheet/formatting/formula behavior stays in
// build_hdfc_workbook.mjs, untouched.
import { buildWorkbook } from './build_hdfc_workbook.mjs';

function getArgValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const workDir = getArgValue('--work-dir');
const outputDir = getArgValue('--output-dir');
const outputPath = getArgValue('--output-path');

if (!workDir || !outputDir || !outputPath) {
  console.error('Usage: node build_hdfc_workbook_cli.mjs --work-dir <dir> --output-dir <dir> --output-path <file>');
  process.exit(2);
}

try {
  await buildWorkbook({ workDir, outputDir, outputPath }, (sheet, current, total) => {
    console.log(JSON.stringify({ type: 'progress', sheet, current, total }));
  });
  process.exit(0);
} catch (err) {
  console.error(JSON.stringify({ type: 'error', error: 'Excel generation failed.', details: err.message }));
  process.exit(1);
}
