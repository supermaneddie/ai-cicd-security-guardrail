#!/usr/bin/env node

'use strict';

function usage() {
  return 'Usage: codeqc-ws-monitor <websocket_url> [--status completed|reviewed]\n' +
    '       codeqc-ws-monitor --websocket_url <websocket_url> [--status completed|reviewed]';
}

function readOptions(args) {
  let websocketUrl = null;
  let status = 'completed';
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === '--websocket_url' || argument === '--websocket-url') {
      websocketUrl = args[index + 1] || null;
      index += 1;
    } else if (argument === '--status') {
      status = args[index + 1] || '';
      index += 1;
    } else if (!argument.startsWith('-') && websocketUrl === null) {
      websocketUrl = argument;
    } else {
      return null;
    }
  }
  if (!websocketUrl || !['completed', 'reviewed'].includes(status)) {
    return null;
  }
  return {websocketUrl, status};
}

const options = readOptions(process.argv.slice(2));
if (!options) {
  console.error(usage());
  process.exit(2);
}
const {websocketUrl, status: expectedStatus} = options;

let parsedUrl;
try {
  parsedUrl = new URL(websocketUrl);
} catch {
  console.error(`Invalid websocket_url: ${websocketUrl}`);
  process.exit(2);
}

if (parsedUrl.protocol !== 'ws:' && parsedUrl.protocol !== 'wss:') {
  console.error(`Invalid websocket_url protocol: ${parsedUrl.protocol}`);
  process.exit(2);
}
parsedUrl.searchParams.set('status', expectedStatus);

const websocket = new WebSocket(parsedUrl);
let opened = false;
let lastReportedStatus = null;
let lastMessage = null;

function valuesEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function formatValue(value) {
  if (typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value);
}

function reportFieldChange(field, previous, current) {
  if (valuesEqual(previous, current) || current === undefined || current === '') {
    return;
  }

  if (previous === undefined || previous === '') {
    console.log(`[${field}] ${formatValue(current)}`);
  } else {
    console.log(`[${field}] ${formatValue(previous)} -> ${formatValue(current)}`);
  }
}

function reportTraceChanges(previousTrace, currentTrace) {
  if (!Array.isArray(currentTrace) || valuesEqual(previousTrace, currentTrace)) {
    return;
  }

  const oldTrace = Array.isArray(previousTrace) ? previousTrace : [];
  let overlap = Math.min(oldTrace.length, currentTrace.length);
  while (overlap > 0 && !valuesEqual(oldTrace.slice(-overlap), currentTrace.slice(0, overlap))) {
    overlap -= 1;
  }

  for (const entry of currentTrace.slice(overlap)) {
    if (entry && typeof entry === 'object') {
      const timestamp = entry.time ? `${entry.time} ` : '';
      console.log(`[trace] ${timestamp}${entry.message || formatValue(entry)}`);
    } else {
      console.log(`[trace] ${formatValue(entry)}`);
    }
  }
}

function handleMessage(data) {
  let message;
  try {
    message = JSON.parse(data);
  } catch {
    console.error('Ignored invalid WebSocket message: expected JSON');
    return;
  }

  if (!message || typeof message !== 'object' || Array.isArray(message)) {
    console.error('Ignored invalid WebSocket message: expected an object');
    return;
  }

  const previous = lastMessage || {};
  reportFieldChange('status', previous.status, message.status);
  reportFieldChange('progress', previous.progress, message.progress);
  reportFieldChange('exception', previous.exception, message.exception);
  reportFieldChange('error', previous.error, message.error);
  reportTraceChanges(previous.trace, message.trace);

  if (
    expectedStatus === 'reviewed' &&
    message.status === 'completed' &&
    previous.status !== 'completed'
  ) {
    console.log('[status] completed; waiting for manual review');
  }

  lastMessage = message;
  if (typeof message.status === 'string') {
    lastReportedStatus = message.status;
  }
}

websocket.addEventListener('open', () => {
  opened = true;
});

websocket.addEventListener('message', (event) => {
  if (typeof event.data === 'string') {
    handleMessage(event.data);
  } else if (event.data instanceof ArrayBuffer) {
    handleMessage(Buffer.from(event.data).toString('utf8'));
  } else {
    console.error('Ignored unsupported binary WebSocket message');
  }
});

websocket.addEventListener('error', (event) => {
  // A close event normally follows and owns process termination.
  console.error(`WebSocket error: ${event.error?.message || 'connection failed'}`);
});

websocket.addEventListener('close', (event) => {
  console.error(
    `WebSocket disconnected (code=${event.code}${event.reason ? `, reason=${event.reason}` : ''})`,
  );

  // Success requires both a clean disconnect and the explicitly requested status.
  process.exitCode = opened && event.code === 1000 && lastReportedStatus === expectedStatus ? 0 : 1;
});
