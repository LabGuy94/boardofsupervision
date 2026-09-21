export type InlineCitation = {
  key: string
  clip_id: number
  meta_id: number
  t0?: number | null
  date?: string | null
  item_title?: string | null
  url?: string | null
}

export type SeekCitation = (clipId: number, metaId: number, seconds?: number) => void

export function CitationChip({ citation, seconds, onSeek }: { citation: InlineCitation; seconds?: number; onSeek: SeekCitation }) {
  const date = citation.date ? new Date(`${citation.date.slice(0, 10)}T12:00:00`) : null
  const label = date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : 'Meeting'
  const time = seconds ?? citation.t0 ?? undefined
  const timestamp = time === undefined ? '' : `${Math.floor(time / 60)}:${String(Math.floor(time % 60)).padStart(2, '0')}`
  return <button type="button" className="citation-chip" title={citation.item_title || 'Watch the meeting evidence'} aria-label={`Watch ${label}${timestamp ? ` at ${timestamp}` : ''}: ${citation.item_title || 'meeting evidence'}`} onClick={() => onSeek(citation.clip_id, citation.meta_id, time)}><span aria-hidden="true">▶</span>{label}{timestamp && ` · ${timestamp}`}</button>
}
