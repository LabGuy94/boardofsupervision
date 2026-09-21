import type { InlineCitation } from '../components/CitationChip'

export type RetrievalStep = {
  n: number
  tool: string
  source: string
  args: Record<string, unknown>
  rows: number
  ms: number
  note?: string
}
export type AnswerDelta = { text: string; citations?: InlineCitation[] }
export type AskStreamOptions<T> = {
  onStep?: (step: RetrievalStep) => void
  onDelta?: (delta: AnswerDelta) => void
  onResult?: (result: T) => void
  onError?: (error: Error) => void
  signal?: AbortSignal
}

/** Read POST SSE across arbitrary UTF-8 and event boundaries. Resolves on done. */
export async function askStream<T = unknown>(body: Record<string, unknown>, options: AskStreamOptions<T> = {}): Promise<T> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    const response = await fetch('/api/ask/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body), signal: options.signal,
    })
    if (!response.ok || !response.body) throw new Error('We couldn’t read the meeting record just now. Please try your question again.')
    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let result: T | undefined
    let receivedResult = false
    let done = false

    function dispatch(event: string) {
      let kind = 'message'
      const data: string[] = []
      for (const line of event.split(/\r?\n/)) {
        if (line.startsWith('event:')) kind = line.slice(6).trim()
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
      }
      if (!data.length) return
      const payload = JSON.parse(data.join('\n'))
      if (kind === 'step') options.onStep?.(payload as RetrievalStep)
      else if (kind === 'answer_delta') options.onDelta?.(payload as AnswerDelta)
      else if (kind === 'result') {
        result = payload as T
        receivedResult = true
        options.onResult?.(result)
      } else if (kind === 'error') {
        throw new Error(typeof payload.message === 'string' ? payload.message : 'The answer stream was interrupted. Please try again.')
      } else if (kind === 'done') done = true
    }

    while (!done) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const event = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        dispatch(event)
      }
      if (chunk.done) {
        if (buffer.trim()) dispatch(buffer)
        break
      }
    }
    if (!done || !receivedResult) throw new Error('The meeting record returned an incomplete answer. Please try again.')
    return result as T
  } catch (caught) {
    const error = caught instanceof Error ? caught : new Error('The answer stream was interrupted. Please try again.')
    if (!options.signal?.aborted) options.onError?.(error)
    throw error
  } finally {
    await reader?.cancel().catch(() => {})
    reader?.releaseLock()
  }
}
