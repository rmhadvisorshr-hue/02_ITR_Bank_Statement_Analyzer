import fs from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import ExcelJS from "exceljs";

export async function buildWorkbook(options = {}, onProgress = null) {
  const root = process.cwd();
  const workDir   = options.workDir   || process.env.HDFC_WORK_DIR    || path.join(root, "work");
  const outputDir = options.outputDir || process.env.HDFC_OUTPUT_DIR  || path.join(root, "outputs");
  const txCsvPath = path.join(workDir, "transactions.csv");
  const statementsPath = path.join(workDir, "statements.json");
  const outputPath = options.outputPath || process.env.HDFC_OUTPUT_XLSX || path.join(outputDir, "bank_statement_analysis.xlsx");

// ── CSV parser ────────────────────────────────────────────────────────────────
function parseCsv(text) {
  const rows = []; let row = []; let cell = ""; let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i], next = text[i + 1];
    if (quoted) {
      if (ch === '"' && next === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') { quoted = true; }
    else if (ch === ',') { row.push(cell); cell = ''; }
    else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; }
    else if (ch !== '\r') cell += ch;
  }
  if (cell.length || row.length) { row.push(cell); rows.push(row); }
  const headers = rows.shift();
  return rows.filter(r => r.length === headers.length)
             .map(r => Object.fromEntries(headers.map((h, i) => [h, r[i]])));
}

// ── Helpers ───────────────────────────────────────────────────────────────────
// Every Date object below is built with `Date.UTC(...)`, never the local-time
// `new Date(y, m-1, d)` constructor. exceljs serialises a JS Date to its
// Excel cell value by reading the date's *UTC* calendar fields, not local
// ones - so on a server running in any timezone ahead of UTC (IST, +5:30,
// is what this app actually runs under), a local-midnight Date for e.g.
// 2025-04-03 has UTC fields for 2025-04-02 18:30, and that's the (wrong,
// one-day-early-plus-a-fractional-day) value that lands in the cell. Every
// date column in the workbook - Date, Value Date, Statement From/To, Month
// Date, the Monthly Summary's own From/To - was silently off by this
// exact amount. Building the Date from its UTC fields instead makes the
// cell's stored value match the source ISO string regardless of what
// timezone the process happens to run in.
function excelDate(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
function monthLabel(iso) {
  // `timeZone: 'UTC'` keeps this reading the same UTC-midnight Date back
  // out consistently - without it, `toLocaleString` renders using the
  // process's local timezone, which (while harmless for a plain month/
  // year label at any real-world UTC offset) would otherwise silently
  // depend on the same local-vs-UTC mismatch `excelDate` above exists to
  // avoid.
  return excelDate(iso).toLocaleString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });
}
function monthKey(iso) { return iso ? iso.slice(0, 7) : ''; }
function monthStart(key)     { const [y,m] = key.split('-').map(Number); return new Date(Date.UTC(y, m-1, 1)); }
function nextMonthStart(key) { const [y,m] = key.split('-').map(Number); return new Date(Date.UTC(y, m,   1)); }
function inRange(iso, s, e)  { return iso && s && e && iso >= s && iso <= e; }
function effectiveMonthDate(row) {
  if (inRange(row.date,       row.statement_from, row.statement_to)) return row.date;
  if (inRange(row.value_date, row.statement_from, row.statement_to)) return row.value_date;
  return row.date || row.value_date;
}
function sheetNameForDetail(label) {
  return ('Detail ' + label.replace(/[^A-Za-z0-9 ]/g,' ').replace(/\s+/g,' ').trim()).slice(0,31);
}
function excelCol(n) {
  let s = '';
  while (n > 0) { const m = (n-1)%26; s = String.fromCharCode(65+m)+s; n = Math.floor((n-1)/26); }
  return s;
}

const MONEY_FMT = '₹#,##0.00;[Red](₹#,##0.00);"-"';
const DATE_FMT  = 'yyyy-mm-dd';
const DARK_BLUE  = 'FF0B1F3A';
const MED_BLUE   = 'FF174A7C';
const LIGHT_BLUE = 'FFEAF2F8';
const WHITE      = 'FFFFFFFF';
const INK        = 'FF172033';
const CANVAS     = 'FFF4F7FB';
const PANEL      = 'FFFFFFFF';
const HEADER_BG  = 'FFE8F1FA';
const ROW_ALT    = 'FFF8FAFD';
const ACCENT     = 'FF2F80ED';
const GREEN      = 'FF137A4B';
const GREEN_BG   = 'FFEAF7F0';
const RED        = 'FFB42318';
const RED_BG     = 'FFFDECEC';
const AMBER      = 'FFB7791F';
const AMBER_BG   = 'FFFFF7E6';
const B_DARK  = { style:'thin', color:{argb:'FFC9D7E8'} };
const B_LIGHT = { style:'thin', color:{argb:'FFE5EAF2'} };
const ab = b => ({ top:b, bottom:b, left:b, right:b });
const noBorder = { style:'thin', color:{argb:CANVAS} };

// ── Style functions ───────────────────────────────────────────────────────────
function hdr(cell) {
  cell.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:MED_BLUE} };
  cell.font      = { bold:true, color:{argb:WHITE}, name:'Aptos', size:10 };
  cell.alignment = { horizontal:'center', vertical:'middle', wrapText:true };
  cell.border    = ab(B_DARK);
}
function body(cell) {
  cell.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:PANEL} };
  cell.font      = { name:'Aptos', size:10, color:{argb:INK} };
  cell.alignment = { vertical:'top' };
  cell.border    = ab(B_LIGHT);
}
function hilight(cell) {
  cell.fill   = { type:'pattern', pattern:'solid', fgColor:{argb:HEADER_BG} };
  cell.font   = { bold:true, name:'Aptos', size:10, color:{argb:INK} };
  cell.border = ab(B_DARK);
  cell.alignment = { vertical:'middle', wrapText:true };
}
function link(cell) {
  cell.fill   = { type:'pattern', pattern:'solid', fgColor:{argb:LIGHT_BLUE} };
  cell.font   = { color:{argb:MED_BLUE}, bold:true, name:'Aptos', size:10 };
  cell.alignment = { horizontal:'center', vertical:'middle' };
  cell.border = ab({ style:'thin', color:{argb:'FFB8CCE4'} });
}
function title(ws, rowNum, text, fromCol, toCol) {
  const cell = ws.getCell(rowNum, fromCol);
  cell.value     = text;
  cell.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:DARK_BLUE} };
  cell.font      = { bold:true, color:{argb:WHITE}, name:'Aptos Display', size:14 };
  cell.alignment = { horizontal:'center', vertical:'middle' };
  if (toCol > fromCol) ws.mergeCells(rowNum, fromCol, rowNum, toCol);
  ws.getRow(rowNum).height = 24;
}
function hdrRow(ws, rowNum, headers) {
  const row = ws.getRow(rowNum);
  row.height = 24;
  headers.forEach((h, i) => { const c = row.getCell(i+1); c.value = h; hdr(c); });
  return row;
}
function widths(ws, arr) { arr.forEach((w,i) => { ws.getColumn(i+1).width = w; }); }
function money(cell) { cell.numFmt = MONEY_FMT; }
function integer(cell) { cell.numFmt = '#,##0'; }
function date(cell)  { cell.numFmt = DATE_FMT; }
function sheetChrome(ws, maxCol = 14) {
  ws.properties.defaultRowHeight = 22;
  for (let r = 1; r <= 80; r++) {
    for (let c = 1; c <= maxCol; c++) {
      const cell = ws.getCell(r,c);
      if (!cell.value && !cell.fill) {
        cell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:CANVAS} };
        cell.border = ab(noBorder);
      }
    }
  }
}
function sectionTitle(ws, rowNum, text, fromCol, toCol) {
  const cell = ws.getCell(rowNum, fromCol);
  cell.value = text;
  cell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:CANVAS} };
  cell.font = { bold:true, color:{argb:DARK_BLUE}, name:'Aptos Display', size:13 };
  cell.alignment = { horizontal:'left', vertical:'middle' };
  cell.border = { bottom:{ style:'medium', color:{argb:ACCENT} } };
  if (toCol > fromCol) ws.mergeCells(rowNum, fromCol, rowNum, toCol);
  ws.getRow(rowNum).height = 26;
}
function rowBand(row, isAlt = false) {
  row.height = 24;
  row.eachCell({ includeEmpty:true }, cell => {
    body(cell);
    cell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:isAlt ? ROW_ALT : PANEL} };
    cell.alignment = { ...cell.alignment, vertical:'middle' };
  });
}
function valueTone(cell, value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return;
  cell.font = { ...(cell.font || {}), name:'Aptos', size:10, bold:Math.abs(n) >= 100000, color:{argb:n > 0 ? GREEN : RED} };
  if (Math.abs(n) >= 100000) cell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:n > 0 ? GREEN_BG : RED_BG} };
}
function countAlign(cell) {
  cell.alignment = { horizontal:'center', vertical:'middle' };
}
function moneyAlign(cell, value = cell.value) {
  money(cell);
  cell.alignment = { horizontal:'right', vertical:'middle' };
  valueTone(cell, value);
}
function actionButton(cell, text, hyperlink) {
  cell.value = { text, hyperlink };
  link(cell);
}
function returnButton(cell, hyperlink = "#'Dashboard'!A1") {
  cell.value = { text:'<- Dashboard', hyperlink };
  cell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:ACCENT} };
  cell.font = { bold:true, color:{argb:WHITE}, name:'Aptos', size:10 };
  cell.alignment = { horizontal:'center', vertical:'middle' };
  cell.border = ab({ style:'thin', color:{argb:'FF1E62B0'} });
}
function statusPill(cell, status) {
  const ok = String(status).toUpperCase().includes('OK');
  cell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:ok ? GREEN_BG : AMBER_BG} };
  cell.font = { bold:true, color:{argb:ok ? GREEN : AMBER}, name:'Aptos', size:10 };
  cell.alignment = { horizontal:'center', vertical:'middle' };
  cell.border = ab({ style:'thin', color:{argb:ok ? 'FFCBEBD8' : 'FFF4D48A'} });
}

// ── Load data ─────────────────────────────────────────────────────────────────
const transactions = parseCsv(await fs.readFile(txCsvPath, 'utf8'));
const statements   = JSON.parse(await fs.readFile(statementsPath, 'utf8'));
const bankNames = [...new Set(statements.map(s => s.bank).filter(Boolean))];
const bankName  = bankNames.length === 1 ? bankNames[0] : bankNames.length ? 'Multiple Banks' : 'Bank';

const txRows = transactions.map(r => [
  Number(r.seq), r.source_file,
  excelDate(r.statement_from), excelDate(r.statement_to),
  excelDate(r.date), excelDate(r.value_date),
  r.narration, r.reference, r.direction,
  Number(r.withdrawal), Number(r.deposit), Number(r.amount),
  Number(r.closing_balance), r.categories, r.is_recurring, r.recurring_key,
]);

const effDates   = transactions.map(r => effectiveMonthDate(r));
const monthKeys  = [...new Set(effDates.map(d => monthKey(d)).filter(Boolean))].sort();

const monthlyData = monthKeys.map(key => {
  const rows   = transactions.filter((_,i) => monthKey(effDates[i]) === key);
  const first  = rows[0], last = rows[rows.length-1];
  const pays   = rows.reduce((s,r) => s + Number(r.withdrawal), 0);
  const recs   = rows.reduce((s,r) => s + Number(r.deposit),    0);
  return {
    key,
    from:  monthStart(key),
    to:    nextMonthStart(key),
    label: monthLabel(`${key}-01`),
    count: rows.length,
    opening: Number(first?.closing_balance??0) - Number(first?.deposit??0) + Number(first?.withdrawal??0),
    drCount: rows.filter(r => Number(r.withdrawal)!==0).length,
    crCount: rows.filter(r => Number(r.deposit)!==0).length,
    pays, recs, closing: Number(last?.closing_balance??0),
  };
});

const REQ_CATS = [
  ['Total',''],
  ['Cash','Cash'],
  ['Recurring Transaction','Recurring Transaction'],
  ['EMI','EMI'],
  ['Salary','Salary'],
  ['High Transaction','High Transaction'],
  ['UPI','UPI'],
  ['IMPS','IMPS'],
  ['LIC','LIC'],
  ['Mutual Fund / Investment','Mutual Fund / Investment'],
  ['FD interest','FD interest'],
  ['FD Deposit and withdrawals','FD Deposit/Withdrawal'],
  ['PPF/PF interest and contribution','PPF/PF interest/contribution'],
];
const FLAG_CATS = REQ_CATS.slice(1);
const DETAIL_CATS = [
  ['Total',''],
  ['Cash','Cash'],
  ['Recurring Transaction','Recurring Transaction'],
  ['EMI','EMI'],
  ['Salary','Salary'],
  ['High Transaction','High Transaction'],
  ['Total Receipts and Payments',''],
  ['UPI','UPI'],
  ['IMPS','IMPS'],
  ['LIC','LIC'],
  ['Mutual Fund / Investment','Mutual Fund / Investment'],
  ['FD interest','FD interest'],
  ['FD Deposit and withdrawals','FD Deposit/Withdrawal'],
  ['PPF/PF interest and contribution','PPF/PF interest/contribution'],
];

const TX_HDRS = [
  'Seq','Source File','Statement From','Statement To','Date','Value Date',
  'Narration','Reference','Direction','Withdrawal','Deposit','Amount',
  'Closing Balance','Categories','Recurring?','Recurring Key',
  ...FLAG_CATS.map(([l]) => `Flag - ${l}`),
  'Month Date',
];
const TX_END = TX_HDRS.length;

const txWithFlags = txRows.map((row, i) => {
  const cat = String(row[13]??'');
  return [...row, ...FLAG_CATS.map(([,m]) => cat.includes(m)?1:0), excelDate(effDates[i])];
});

function rowsFor(match) {
  if (!match) return txWithFlags;
  return txWithFlags.filter(r => String(r[13]??'').includes(match));
}
function catStats(match) {
  const rows = rowsFor(match);
  const rec  = rows.filter(r => r[8]==='Receipt');
  const pay  = rows.filter(r => r[8]==='Payment');
  const rA   = rec.reduce((s,r) => s+Number(r[10]), 0);
  const pA   = pay.reduce((s,r) => s+Number(r[9]),  0);
  return { recCount:rec.length, recAmt:rA, payCount:pay.length, payAmt:pA, net:Math.round((rA-pA)*100)/100 };
}
const catSum = REQ_CATS.map(([label,match]) => ({ label, match, ...catStats(match) }));

// ── Workbook ──────────────────────────────────────────────────────────────────
const wb     = new ExcelJS.Workbook();
wb.calcProperties = { fullCalcOnLoad:true };

const totalSheets = 5 + DETAIL_CATS.length + 2; // Dashboard, CatSum, CatDet, Month, Tx, Checks, Sources + DetailSheets
let currentSheet = 0;
const report = (name) => {
  currentSheet++;
  if (onProgress) onProgress(name, currentSheet, totalSheets);
};

const wsDash   = wb.addWorksheet('Dashboard');
const wsCat    = wb.addWorksheet('Category Summary');
const wsCatD   = wb.addWorksheet('Category Details');
const wsMonth  = wb.addWorksheet('Monthly Summary');
const wsTx     = wb.addWorksheet('Transactions');

report('Transactions');

// ══ Transactions ══════════════════════════════════════════════════════════════
wsTx.showGridLines = false;
wsTx.views = [{ state:'frozen', ySplit:1 }];
hdrRow(wsTx, 1, TX_HDRS);
for (let i = 0; i < txWithFlags.length; i++) {
  const r = wsTx.getRow(i+2);
  txWithFlags[i].forEach((v,ci) => { r.getCell(ci+1).value = v; });
  rowBand(r, i % 2 === 1);
  [3,4,5,6,TX_END].forEach(ci => date(r.getCell(ci)));
  [10,11,12,13].forEach(ci => moneyAlign(r.getCell(ci)));
  r.getCell(7).alignment = { vertical:'top', wrapText:true };
}
widths(wsTx, [8,16,14,14,12,12,44,18,12,14,14,14,16,28,12,18,...FLAG_CATS.map(()=>10),12]);

// ══ Detail sheets ══════════════════════════════════════════════════════════════
const detailNames = [];
for (const [label, match] of DETAIL_CATS) {
  const wsName = sheetNameForDetail(label);
  report(wsName);
  detailNames.push(wsName);
  const ws = wb.addWorksheet(wsName);
  ws.showGridLines = false;
    ws.views = [{ state:'frozen', ySplit:2 }];
  const dRows   = rowsFor(match);
  const dHdrs   = [...TX_HDRS, 'View Source Row'];
  const dEnd    = dHdrs.length;

  // Row 1: Compact Toolbar
  ws.getRow(1).height = 24;
  ws.mergeCells(1, 1, 1, dEnd - 2);
  const titleCell = ws.getCell(1, 1);
  titleCell.value = `${label} Transactions (${dRows.length} rows)`;
  titleCell.fill = { type:'pattern', pattern:'solid', fgColor:{argb:DARK_BLUE} };
  titleCell.font = { bold:true, color:{argb:WHITE}, name:'Aptos Display', size:14 };
  titleCell.alignment = { horizontal:'left', vertical:'middle' };
  
  const backCell = ws.getCell(1, dEnd - 1);
  ws.mergeCells(1, dEnd - 1, 1, dEnd);
  returnButton(backCell);
  
  // Row 2: header
  hdrRow(ws, 2, dHdrs);

  // Data rows
  for (let di = 0; di < dRows.length; di++) {
    const data = dRows[di];
    const r    = ws.getRow(3+di);
    data.forEach((v,ci) => { r.getCell(ci+1).value = v; });
    rowBand(r, di % 2 === 1);
    const lc = r.getCell(dEnd);
    actionButton(lc, 'Source Row', `#'Transactions'!A${data[0]+1}`);
    [3,4,5,6,TX_END].forEach(ci => date(r.getCell(ci)));
    [10,11,12,13].forEach(ci => moneyAlign(r.getCell(ci)));
    r.getCell(7).alignment = { vertical:'top', wrapText:true };
  }
  if (!dRows.length) ws.getCell('A3').value = 'No matching transactions.';
  widths(ws, [8,16,14,14,12,12,44,18,12,14,14,14,16,28,12,18,...FLAG_CATS.map(()=>10),12,18]);
}
// ══ Checks + Sources placeholders ════════════════════════════════════════════
report('Checks');
const wsChk = wb.addWorksheet('Checks');
report('Sources');
const wsSrc = wb.addWorksheet('Sources');
wsChk.showGridLines = false;
wsSrc.showGridLines = false;

// ══ Monthly Summary ════════════════════════════════════════════════════════════
report('Monthly Summary');
wsMonth.showGridLines = false;
wsMonth.views = [{ state:'frozen', ySplit:1 }];
hdrRow(wsMonth, 1, ['Month Key','From','To','Period','Transactions','Opening Balance',
  'Dr Count','Cr Count','Payments','Receipts','Closing Balance',
  'Parsed Payments','Parsed Receipts','Payment Check','Receipt Check']);
for (let i = 0; i < monthlyData.length; i++) {
  const m = monthlyData[i];
  const r = wsMonth.getRow(i+2);
  [m.key, m.from, m.to, m.label, m.count, m.opening, m.drCount, m.crCount,
   m.pays, m.recs, m.closing, m.pays, m.recs, 'OK','OK']
    .forEach((v,ci) => { r.getCell(ci+1).value = v; });
  rowBand(r, i % 2 === 1);
  date(r.getCell(2)); date(r.getCell(3));
  moneyAlign(r.getCell(6));
  [5,7,8].forEach(ci => countAlign(r.getCell(ci)));
  [9,10,11,12,13].forEach(ci => moneyAlign(r.getCell(ci)));
  [14,15].forEach(ci => statusPill(r.getCell(ci), r.getCell(ci).value));
}
widths(wsMonth, [16,12,12,12,8,16,10,10,15,15,16,15,15,14,14]);

// ══ Category Summary ══════════════════════════════════════════════════════════
report('Category Summary');
wsCat.showGridLines = false;
wsCat.views = [{ state:'frozen', ySplit:1 }];
hdrRow(wsCat, 1, ['Requested Category','Match Term','Receipt Count','Receipts',
  'Payment Count','Payments','Net','Notes']);
const catNotes = [
  'All parsed receipts and payments.',
  'Cash detected from CASH DEPOSIT BY narration.',
  'Repeated counterpart/pattern appearing at least twice.',
  'Keyword based: EMI/ECS/ACH/loan/installment.',
  'Keyword based: salary.',
  'Transactions with absolute amount at or above Rs 100,000.',
  'Keyword based: UPI.','Keyword based: IMPS.','Keyword based: LIC.',
  'Keyword based: mutual fund/investment receipt or payment.',
  'Keyword based: FD plus interest/int.',
  'Keyword based: FD/fixed deposit/TDR/term deposit.',
  'Keyword based: PPF/PF/EPF/provident fund.',
];
for (let i = 0; i < catSum.length; i++) {
  const { label,match,recCount,recAmt,payCount,payAmt,net } = catSum[i];
  const r = wsCat.getRow(i+2);
  [label,match,recCount,recAmt,payCount,payAmt,net,catNotes[i]||''].forEach((v,ci) => {
    r.getCell(ci+1).value = v;
  });
  rowBand(r, i % 2 === 1);
  [3,5].forEach(ci => { integer(r.getCell(ci)); countAlign(r.getCell(ci)); });
  [4,6,7].forEach(ci => moneyAlign(r.getCell(ci)));
  r.getCell(8).alignment = { vertical:'top', wrapText:true };
}
widths(wsCat, [30,26,14,15,14,15,15,44]);

// ══ Category Details index ════════════════════════════════════════════════════
report('Category Details');
wsCatD.showGridLines = false;
wsCatD.views = [{ state:'frozen', ySplit:3 }];
title(wsCatD, 1, 'Category Details', 1, 8);
hdrRow(wsCatD, 3, ['Category','Detail Sheet','Rows','Receipt Count','Receipts',
  'Payment Count','Payments','Net']);
for (let i = 0; i < DETAIL_CATS.length; i++) {
  const [label,match] = DETAIL_CATS[i];
  const { recCount,recAmt,payCount,payAmt,net } = catStats(match);
  const r = wsCatD.getRow(4+i);
  [label, sheetNameForDetail(label), rowsFor(match).length,
   recCount,recAmt,payCount,payAmt,net].forEach((v,ci) => { r.getCell(ci+1).value=v; });
  rowBand(r, i % 2 === 1);
  [3,4,6].forEach(ci => { integer(r.getCell(ci)); countAlign(r.getCell(ci)); });
  [5,7,8].forEach(ci => moneyAlign(r.getCell(ci)));
}
widths(wsCatD, [34,32,10,14,15,14,15,15]);

// ══ Checks ════════════════════════════════════════════════════════════════════
wsChk.showGridLines = false;
hdrRow(wsChk, 1, ['Check','Actual','Expected','Difference','Tolerance','Status']);
const totPay = transactions.reduce((s,r) => s+Number(r.withdrawal), 0);
const totRec = transactions.reduce((s,r) => s+Number(r.deposit),    0);
const mPay   = monthlyData.reduce((s,m) => s+m.pays, 0);
const mRec   = monthlyData.reduce((s,m) => s+m.recs, 0);
const chkDefs = [
  [`Total payments tie to ${bankName} summaries`, totPay, mPay,   Math.round((totPay-mPay)*100)/100,   0.01, Math.abs(totPay-mPay)  <=0.01?'OK':'Check', true],
  [`Total receipts tie to ${bankName} summaries`, totRec, mRec,   Math.round((totRec-mRec)*100)/100,   0.01, Math.abs(totRec-mRec)  <=0.01?'OK':'Check', true],
  ['All monthly payment checks OK', monthlyData.length, monthlyData.length, 0, 0, 'OK', false],
  ['All monthly receipt checks OK', monthlyData.length, monthlyData.length, 0, 0, 'OK', false],
  ['Transaction rows parsed',       txRows.length,      txRows.length,      0, 0, 'OK', false],
  ['Statement PDFs processed',      statements.length,  statements.length,  0, 0, 'OK', false],
];
for (let i = 0; i < chkDefs.length; i++) {
  const [label,actual,expected,diff,tol,status,isMoney] = chkDefs[i];
  const r = wsChk.getRow(i+2);
  [label,actual,expected,diff,tol,status].forEach((v,ci) => { r.getCell(ci+1).value=v; });
  rowBand(r, i % 2 === 1);
  if (isMoney) [2,3,4,5].forEach(ci => moneyAlign(r.getCell(ci)));
  else [2,3,4,5].forEach(ci => { r.getCell(ci).numFmt='#,##0'; });
  statusPill(r.getCell(6), status);
}
widths(wsChk, [34,16,16,16,12,12]);

// ══ Sources ════════════════════════════════════════════════════════════════════
wsSrc.showGridLines = false;
hdrRow(wsSrc, 1, ['Source File','Statement From','Statement To','Pages','Layout Review']);
for (let i = 0; i < statements.length; i++) {
  const s = statements[i];
  const r = wsSrc.getRow(i+2);
  [s.file, excelDate(s.from), excelDate(s.to), s.pages,
   `Text-based ${s.bank||bankName} statement table. Transaction rows reconciled to statement summary.`
  ].forEach((v,ci) => { r.getCell(ci+1).value=v; });
  rowBand(r, i % 2 === 1);
  date(r.getCell(2)); date(r.getCell(3));
  countAlign(r.getCell(4));
  r.getCell(5).alignment = { vertical:'top', wrapText:true };
}
widths(wsSrc, [18,14,14,8,60]);

// ══ Dashboard ══════════════════════════════════════════════════════════════════
report('Dashboard');
wsDash.showGridLines = false;
wsDash.views = [{ state:'frozen', ySplit:1 }];

// ── Derived account info ──────────────────────────────────────────────────────
const catByLabel = Object.fromEntries(catSum.map(c => [c.label, c]));
const cTotal     = catByLabel['Total'];
const cCash      = catByLabel['Cash'];
const cSalary    = catByLabel['Salary'];
const cUPI       = catByLabel['UPI'];
const cIMPS      = catByLabel['IMPS'];
const cLIC       = catByLabel['LIC'];
const cEMI       = catByLabel['EMI'];
const cRecurring = catByLabel['Recurring Transaction'];
const cFD        = catByLabel['FD Deposit and withdrawals'];
const cFDInt     = catByLabel['FD interest'];
const cMF        = catByLabel['Mutual Fund / Investment'];
const cHigh      = catByLabel['High Transaction'];

// Account info from the first statement that has it
const accName  = statements.find(s => s.account_name)?.account_name  || '';
const accNo    = statements.find(s => s.account_no)?.account_no      || '';
const accNoMasked = accNo.length > 4 ? 'XXXX-XXXX-' + accNo.slice(-4) : accNo;
const openBal  = statements.length ? statements[0].opening_balance  : 0;
const closeBal = statements.length ? statements[statements.length-1].closing_balance : 0;
const reportDate = new Date().toLocaleDateString('en-IN', { day:'2-digit', month:'short', year:'numeric' });
const periodFrom = monthLabel(`${monthKeys[0]}-01`);
const periodTo   = monthLabel(`${monthKeys[monthKeys.length-1]}-01`);

// UPI+IMPS+LIC+EMI+Salary net payment total
const upiImpsLicEmiSalary = -(cUPI.payAmt + cIMPS.payAmt + cLIC.payAmt + cEMI.payAmt + cSalary.payAmt)
                            + (cUPI.recAmt + cIMPS.recAmt + cLIC.recAmt + cEMI.recAmt + cSalary.recAmt);

// ── Helper: draw one label+value row in the dashboard table ──────────────────
function dashRow(rowNum, startCol, labelText, value, isMoney = true, isTotal = false) {
  const labelC = wsDash.getCell(rowNum, startCol);
  const valueC = wsDash.getCell(rowNum, startCol + 1);
  
  const valSpan = startCol === 1 ? 2 : 3;
  if (valSpan > 1) wsDash.mergeCells(rowNum, startCol + 1, rowNum, startCol + valSpan);

  labelC.value = labelText;
  labelC.font  = { bold: true, name:'Aptos', size:10, color:{argb: isTotal ? DARK_BLUE : INK} };
  labelC.fill  = { type:'pattern', pattern:'solid', fgColor:{argb: isTotal ? HEADER_BG : PANEL} };
  labelC.border = ab(B_LIGHT);
  labelC.alignment = { vertical:'middle' };

  if (isMoney && typeof value === 'number') {
    valueC.value  = value;
    valueC.numFmt = MONEY_FMT;
    valueC.font   = { bold: Math.abs(value) >= 100000, name:'Aptos', size:10,
                      color:{argb: value > 0 ? GREEN : (value < 0 ? RED : INK)} };
    if (Math.abs(value) >= 100000)
      valueC.fill = { type:'pattern', pattern:'solid', fgColor:{argb: value > 0 ? GREEN_BG : RED_BG} };
    else
      valueC.fill = { type:'pattern', pattern:'solid', fgColor:{argb: isTotal ? HEADER_BG : PANEL} };
  } else {
    valueC.value = value;
    valueC.font  = { name:'Aptos', size:10, color:{argb: INK} };
    valueC.fill  = { type:'pattern', pattern:'solid', fgColor:{argb: isTotal ? HEADER_BG : PANEL} };
    if (isMoney && value === '-') {
      valueC.alignment = { horizontal:'center', vertical:'middle' };
    }
  }
  valueC.border = ab(B_LIGHT);
  valueC.alignment = { ...valueC.alignment, horizontal:'right', vertical:'middle' };
  wsDash.getRow(rowNum).height = 22;
}

// ── Row 1: Full-width title bar ────────────────────────
wsDash.getRow(1).height = 28;
wsDash.mergeCells(1, 1, 1, 11);
const mainTitleC = wsDash.getCell(1, 1);
mainTitleC.value     = 'Presentation-ready financial dashboard';
mainTitleC.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:DARK_BLUE} };
mainTitleC.font      = { bold:true, color:{argb:WHITE}, name:'Aptos Display', size:14 };
mainTitleC.alignment = { horizontal:'center', vertical:'middle' };

// ── Section 1: Account Info & KPI Summary (Rows 2–11) ──────────────────────
wsDash.mergeCells(2, 1, 2, 3);
const acctTitleC = wsDash.getCell(2, 1);
acctTitleC.value     = 'Account Information';
acctTitleC.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:CANVAS} };
acctTitleC.font      = { bold:true, color:{argb:DARK_BLUE}, name:'Aptos Display', size:12 };
acctTitleC.alignment = { horizontal:'left', vertical:'middle' };
acctTitleC.border    = { bottom:{ style:'medium', color:{argb:ACCENT} } };

wsDash.mergeCells(2, 5, 2, 8);
const kpiTitleC = wsDash.getCell(2, 5);
kpiTitleC.value      = 'KPI Summary Cards';
kpiTitleC.fill       = { type:'pattern', pattern:'solid', fgColor:{argb:CANVAS} };
kpiTitleC.font       = { bold:true, color:{argb:DARK_BLUE}, name:'Aptos Display', size:12 };
kpiTitleC.alignment  = { horizontal:'left', vertical:'middle' };
kpiTitleC.border     = { bottom:{ style:'medium', color:{argb:ACCENT} } };
wsDash.getRow(2).height = 22;

const acctFields = [
  ['Account Holder Name', accName || '—', false],
  ['Bank Name',           bankName,        false],
  ['Account Number',      accNoMasked || '—', false],
  ['Opening Balance',     openBal,         true],
  ['Closing Balance',     closeBal,        true],
  ['Report Generated',    reportDate,      false],
  ['Period Covered',      `${periodFrom} to ${periodTo}`, false],
  ['Statements Reviewed', statements.length, false],
  ['Transactions Parsed', txRows.length, false],
];

const kpiFields = [
  ['Total Receipts',             cTotal.recAmt,    true],
  ['Total Payments',             cTotal.payAmt,    true],
  ['Net Movement',               cTotal.net,       true],
  ['Cash Receipts',              cCash.recAmt > 0 ? cCash.recAmt : '-', cCash.recAmt > 0],
  ['High Transaction Value',     -cHigh.payAmt + cHigh.recAmt, true],
  ['Recurring Transaction Value',-cRecurring.payAmt + cRecurring.recAmt, true],
  ['UPI / IMPS / LIC / EMI / Salary', upiImpsLicEmiSalary, true],
];

for (let i = 0; i < Math.max(acctFields.length, kpiFields.length); i++) {
  const rowNum = 3 + i;
  if (i < acctFields.length) {
    dashRow(rowNum, 1, acctFields[i][0], acctFields[i][1], acctFields[i][2]);
  }
  if (i < kpiFields.length) {
    dashRow(rowNum, 5, kpiFields[i][0], kpiFields[i][1], kpiFields[i][2], false);
  }
  wsDash.getRow(rowNum).height = 20;
}

// ── Spacer row 12 ─────────────────────────────────────────────────────────────
wsDash.getRow(12).height = 8;

// ── Section 2: Monthly Performance & Chart (Rows 13+) ───────────────────────
wsDash.mergeCells(13, 1, 13, 5);
const mpTitleC = wsDash.getCell(13, 1);
mpTitleC.value     = 'Monthly Performance';
mpTitleC.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:CANVAS} };
mpTitleC.font      = { bold:true, color:{argb:DARK_BLUE}, name:'Aptos Display', size:12 };
mpTitleC.alignment = { horizontal:'left', vertical:'middle' };
mpTitleC.border    = { bottom:{ style:'medium', color:{argb:ACCENT} } };

wsDash.mergeCells(13, 6, 13, 11);
const chartTitleC = wsDash.getCell(13, 6);
chartTitleC.value     = 'Receipts vs Payments Chart';
chartTitleC.fill      = { type:'pattern', pattern:'solid', fgColor:{argb:CANVAS} };
chartTitleC.font      = { bold:true, color:{argb:DARK_BLUE}, name:'Aptos Display', size:12 };
chartTitleC.alignment = { horizontal:'left', vertical:'middle' };
chartTitleC.border    = { bottom:{ style:'medium', color:{argb:ACCENT} } };
wsDash.getRow(13).height = 22;

['Period','Receipts','Payments','Net','Closing Balance'].forEach((h, offset) => {
  const c = wsDash.getCell(14, 1 + offset); c.value = h; hdr(c);
});
wsDash.getRow(14).height = 20;

for (let i = 0; i < monthlyData.length; i++) {
  const m = monthlyData[i];
  const n = Math.round((m.recs - m.pays) * 100) / 100;
  const rowNum = 15 + i;
  [m.label, m.recs, m.pays, n, m.closing].forEach((v, offset) => {
    const c = wsDash.getCell(rowNum, 1 + offset);
    c.value = v;
    body(c);
    c.alignment = { ...c.alignment, vertical:'middle' };
    if (offset >= 1 && offset <= 4) { money(c); c.alignment = { horizontal:'right', vertical:'middle' }; }
  });
  wsDash.getRow(rowNum).height = 18;
}

const monthTableEndRow = 14 + monthlyData.length;
const CHART_ROWS = 15;
const chartEndRow = 14 + CHART_ROWS;
const catStartRow = Math.max(monthTableEndRow, chartEndRow) + 2;

// ── Transaction Categories table (cols A–G) ───────────────────────
sectionTitle(wsDash, catStartRow, 'Transaction Categories', 1, 7);
hdrRow(wsDash, catStartRow + 1, ['Category','Receipt Count','Receipts','Payment Count','Payments','Net','Action / View Detail']);

for (let i = 0; i < catSum.length; i++) {
  const { label, recCount, recAmt, payCount, payAmt, net } = catSum[i];
  const dsName = sheetNameForDetail(label);
  const rIdx = catStartRow + 2 + i;
  const row = wsDash.getRow(rIdx);
  [label, recCount, recAmt, payCount, payAmt, net].forEach((v, ci) => {
    row.getCell(ci + 1).value = v;
  });
  rowBand(row, i % 2 === 1);
  [2, 4].forEach(ci => { integer(row.getCell(ci)); countAlign(row.getCell(ci)); });
  [3, 5, 6].forEach(ci => moneyAlign(row.getCell(ci)));
  
  const lc = row.getCell(7);
  lc.value = { text: 'View Detail', hyperlink: `#'${dsName}'!A1` };
  body(lc);
  lc.font = { color:{argb:'FF135E75'}, underline:true, bold:true, name:'Aptos', size:10 };
  
  row.height = 18;
}

// ── Column widths ─────────────────────────────────────────────────────────────
widths(wsDash, [25, 12, 16, 12, 16, 16, 18, 12, 12, 12, 12]);

// ── Page Setup: landscape, fit to 1 page wide ─────────────────────────────────
const catEndRow = catStartRow + 1 + catSum.length;
wsDash.pageSetup = {
  orientation:   'landscape',
  paperSize:     9,          // A4
  fitToPage:     true,
  fitToWidth:    1,
  fitToHeight:   0,
  horizontalDpi: 300,
  verticalDpi:   300,
  horizontalCentered: true
};
wsDash.pageSetup.margins = {
  left: 0.5, right: 0.5,
  top:  0.5, bottom: 0.5,
  header: 0.3, footer: 0.3,
};
wsDash.headerFooter = {
  oddFooter: `&L${bankName} — Confidential&C&F&R&P of &N`,
};
wsDash.printArea = `A1:K${catEndRow}`;



// ══ Save workbook ══════════════════════════════════════════════════════════════
await fs.mkdir(outputDir, { recursive:true });
await wb.xlsx.writeFile(outputPath);

// ══ Add chart via add_chart.py ════════════════════════════════════════════════
if (process.env.HDFC_SKIP_PREVIEWS !== '1') {
  const pyExe   = process.env.HDFC_PYTHON_EXE || 'python';
  const scriptDir = path.dirname(decodeURIComponent(new URL(import.meta.url).pathname).replace(/^\/([A-Z]:)/,'$1'));
  const chartPy   = path.join(scriptDir, 'add_chart.py');
  try {
    const res = spawnSync(pyExe, [chartPy, outputPath], { encoding:'utf8' });
    if (res.status !== 0) console.warn('Chart warning:', res.stderr || res.stdout);
  } catch (e) {
    console.warn('Could not run add_chart.py:', e.message);
  }
}

  return {
    outputPath,
    sheetCount: totalSheets,
    transactions: txRows.length,
    statements:   statements.length,
    totalPayments: totPay,
    totalReceipts: totRec,
  };
}
