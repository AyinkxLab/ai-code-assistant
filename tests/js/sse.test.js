import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import { SSEClient } from '../../static/js/sse.js';

describe('SSEClient', () => {
  let fetchMock;
  let originalFetch;

  beforeEach(() => {
    originalFetch = global.fetch;
    fetchMock = vi.fn();
    global.fetch = fetchMock;
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  function makeStream(chunks) {
    const encoder = new TextEncoder();
    let i = 0;
    return {
      getReader() {
        return {
          read() {
            if (i >= chunks.length) {
              return Promise.resolve({ done: true, value: undefined });
            }
            const value = encoder.encode(chunks[i++]);
            return Promise.resolve({ done: false, value });
          },
          cancel() {
            return Promise.resolve();
          },
        };
      },
      body: { cancel() { return Promise.resolve(); } },
    };
  }

  it('parses a single event from the stream', async () => {
    fetchMock.mockResolved({
      ok: true,
      status: 200,
      body: makeStream(['data: {"type":"delta","text":"hi"}\n\n']),
    });

    const client = new SSEClient('/api/chat');
    const events = [];
    client.on('message', (ev) => events.push(ev));
    await client.connect();

    expect(events).length(1);
    expect(events[0]).equals({ type: 'delta', text: hi' });
  });

  it('parses multiple events across chunk boundaries', async () => {
    fetchMock.mockResolved({
      ok: true,
      status: 200,
      body: makeStream([
        'data: {"type":"delta","text":"a"',
        '\n\ndata: {"type":"delta","text":"b"}\n\n',
      ]),
    });

    const client = new SSEClient('/api/chat');
    const events = [];
    client.on('message', (ev) => events.push(ev));
    await client.connect();

    expect(events).length(2);
    expect(events[0].text).toBe('a');
    expect(events[1].text).toBe('b');
  });

  it('ignores comment lines and keeps event type from event: field', async () => {
    fetchMock.mockResolved({
      ok: true,
      status: 200,
      body: makeStream([
        ': keep-alive\nevent: done\ndata: {"done":true}\n\n',
      ]),
    });

    const client = new SSEClient('/api/chat');
    const events = [];
    client.on('message', (ev) => events.push(ev));
    await client.connect();

    expect(events).length(1);
    expect(events[0].event).toBe('done');
    expect(events[0].data).toEqual({ done: true });
  });

  it('reconnects with Last-Event-ID after a network failure', async () => {
    fetchMock
      .mockRejectedOnce(new TypeError('network error'))
      .mockResolvedOnce({
        ok: true,
        status: 200,
        body: makeStream(['id: 42\ndata: {"type":"delta","text":"hi"}\n\n']),
      });

    const client = new SSEClient('/api/chat', { reconnectDelayMs: 0 });
    const events = [];
    client.on('message', (ev) => events.push(ev));
    await client.connect();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondCall = fetchMock.mock.calls[1];
    expect(secondCall[1].headers.get('Last-Event-ID')).toBe('42');
    expect(events).length(1);
  });

  it('cancels the in-flight stream', async () => {
    const cancel = vi.fn();
    fetchMock.mockResolved({
      ok: true,
      status: 200,
      body: {
        getReader() {
          return {
            read() {
              return new Promise(() => {});
            },
            cancel,
          };
        },
        cancel,
      },
    });

    const client = new SSEClient('/api/chat');
    const promise = client.connect();
    client.cancel();
    await promise;

    expect(cancel).toHaveBeenCalled();
  });

  it('emits an error event on non-200 response', async () => {
    fetchMock.mockResolved({
      ok: false,
      status: 500,
      body: makeStream(['server error']),
    });

    const client = new SSEClient('/api/chat');
    const errors = [];
    client.on('error', (err) => errors.push(err));
    await client.connect();

    expect(errors).length(1);
    expect(errors[0].status).toBe(500);
  });

  it('stops reconnecting after cancel', async () => {
    fetchMock.mockResolved({
      ok: true,
      status: 200,
      body: makeStream([], {
        getReader() {
          return {
            read() {
              return Promise.reject(new TypeError('network error'));
            },
            cancel() { return Promise.resolve(); },
          };
        },
      }),
    });

    const client = new SSEClient('/api/chat', { reconnectDelayMs: 0 });
    const promise = client.connect();
    client.cancel();
    await promise;
    await new Promise((resolve) => setTimeout(resolve, 5));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
