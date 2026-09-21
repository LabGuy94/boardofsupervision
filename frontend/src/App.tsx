import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AnswerMarkdown } from './components/AnswerMarkdown'
import type { InlineCitation, SeekCitation } from './components/CitationChip'
import { askStream, type RetrievalStep } from './lib/askStream'
import './styles/prose.css'
import MeetingFilter, { getMeetingRangeLabel } from './components/MeetingFilter'
import SyncedTranscript from './components/SyncedTranscript'
import housingFixture from './fixtures/ask_housing_supervisors.json'
import meetingOverviewFixture from './fixtures/ask_meeting_overview.json'
import settlementsFixture from './fixtures/ask_settlements.json'
import streetTreesFixture from './fixtures/ask_street_trees.json'

type Quote = { name?: string | null; text?: string | null; t0?: number | null }
type Vote = { name: string; vote: string; inferred?: boolean }
type Evidence = {
  file_no?: string | null
  start_sec?: number | null
  end_sec?: number | null
  outcome?: string | null
  topics?: string[] | null
  votes?: Vote[] | null
  vote_tally?: Record<string, number> | null
  quotes?: Quote[] | null
  video_mp4?: string | null
}
type Contact = { name?: string | null; district?: number | null; phone?: string | null; email?: string | null; profile_url?: string | null }
type DocumentLink = { label?: string; url: string; source_url?: string }
type Legislation = {
  sponsors?: string[]
  fiscal_impact?: boolean | null
  phase?: string | null
  history?: { date?: string; action: string; body?: string; source_url?: string }[]
  committee?: { name: string; current_members?: { name: string; role?: string | null }[]; clerk?: Contact | null; source_url?: string } | null
  your_supervisor_on_committee?: boolean | null
  documents?: DocumentLink[]
  source_url?: string | null
}
type Action = {
  type?: string
  file_no?: string | null
  body?: string | null
  date?: string | null
  time?: string | null
  location?: string | null
  agenda_item?: string | null
  source_url?: string | null
  agenda_url?: string | null
  participation?: {
    in_person?: string | null
    remote?: string | null
    remote_reason?: string | null
    written?: string | null
    written_comment?: { deadline?: string | null; email?: string | null; committee_clerk_email?: string | null; mailing_address?: string | null } | null
  } | null
  name?: string | null
  district?: number | null
  phone?: string | null
  email?: string | null
  profile_url?: string | null
  tty?: string | null
  deadline?: string | null
  languages?: string[] | null
  committee?: string | null
  final_passage_date?: string | null
  election_date?: string | null
  letter?: string | null
  title?: string | null
  documents?: DocumentLink[]
}
type Citation = {
  text: string
  clip_id?: number | null
  meta_id?: number | null
  t0?: number | null
  date?: string | null
  item_title?: string | null
  url?: string | null
  evidence?: Evidence | null
  actions?: Action[] | null
  legislation?: Legislation | null
}
type Supervisor = { district?: number; name: string; phone?: string | null; email?: string | null; profile_url?: string | null }
type AskResponse = {
  answer: string
  bullets: Citation[]
  citations?: InlineCitation[]
  suggestions?: string[] | null
  retrieval: { mode?: string; template?: string | null; cypher?: string | null; rows?: Record<string, unknown>[]; steps?: RetrievalStep[] }
  latency_ms?: number
  supervisor?: Supervisor | null
}
type Meeting = { clip_id: number; date: string; n_items: number; n_cues: number }
type Location = { lat: number; lon: number; fallback: boolean }
type Selection = { index: number; seconds: number; play: boolean; request: number }

const SUGGESTIONS = [
  { label: 'Street trees', question: 'What did the Board decide about street trees on Sept 15?' },
  { label: 'Housing', question: 'Which supervisors spoke most about housing this month?' },
  { label: 'City spending', question: 'How much in lawsuit settlements did the City approve on Sept 15, and for what?' },
  { label: 'Bonds & public projects', question: 'What bonds or certificates of participation did the Board approve on September 15?' },
]
const EXAMPLE_LOOKUPS = [
  'what passed on street trees',
  'who voted against the housing bond',
  'what the public said about the Great Highway',
  'how much was paid in settlements this summer',
  'when the Open Space bonds come back',
  'what Jackie Fielder said about homelessness',
]
const USE_MOCK = new URLSearchParams(window.location.search).get('mock') === '1'
const OUTCOMES: Record<string, string> = { passed: 'Passed', failed: 'Failed', continued: 'Continued', referred: 'Referred to committee', no_action: 'No action', unknown: 'Outcome not recorded' }
const CITY_HALL: Location = { lat: 37.7793, lon: -122.4193, fallback: true }

function ExampleLookup({ onPick }: { onPick: (example: string) => void }) {
  const [index, setIndex] = useState(0)
  useEffect(() => {
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)')
    let timer: number | undefined
    const update = () => {
      window.clearInterval(timer)
      if (motion.matches) setIndex(0)
      else timer = window.setInterval(() => setIndex((current) => (current + 1) % EXAMPLE_LOOKUPS.length), 2500)
    }
    update()
    motion.addEventListener('change', update)
    return () => { window.clearInterval(timer); motion.removeEventListener('change', update) }
  }, [])
  return <div className="example-lookup"><span>Try asking</span><button type="button" onClick={() => onPick(EXAMPLE_LOOKUPS[index])}><span className="lookup-text" key={index}>{EXAMPLE_LOOKUPS[index]}</span></button></div>
}

function readableText(text: string): string {
  return text.replace(/\s*\u2014\s*/g, ', ')
}

function fixtureFor(question: string): AskResponse {
  if (/settlement|lawsuit/i.test(question)) return settlementsFixture as AskResponse
  if (/housing|supervisor/i.test(question)) return housingFixture as AskResponse
  if (/what happened/i.test(question)) return meetingOverviewFixture as AskResponse
  if (/street tree/i.test(question)) return streetTreesFixture as AskResponse
  return { answer: 'There is no saved demo answer for this question. Turn off demo mode to ask the live meeting record.', bullets: [], retrieval: { mode: 'mock', rows: [] } }
}

function isAskResponse(value: unknown): value is AskResponse {
  if (!value || typeof value !== 'object') return false
  const result = value as Record<string, unknown>
  return typeof result.answer === 'string' && Array.isArray(result.bullets) &&
    result.bullets.every((bullet) => bullet && typeof bullet.text === 'string') &&
    !!result.retrieval && typeof result.retrieval === 'object'
}

function dateLabel(date: string | null | undefined, style: 'full' | 'short' | 'weekday' = 'full'): string {
  if (!date) return ''
  const parsed = new Date(`${date}T00:00:00Z`)
  if (Number.isNaN(parsed.valueOf())) return date
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', timeZone: 'UTC',
    ...(style === 'full' ? { year: 'numeric' } : {}),
    ...(style === 'weekday' ? { weekday: 'long' } : {}),
  }).format(parsed)
}

function timeLabel(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

function startTime(bullet: Citation): number {
  const seconds = bullet.evidence?.start_sec ?? bullet.t0 ?? 0
  return Number.isFinite(seconds) ? Math.max(0, seconds) : 0
}

function corpusLabel(meetings: Meeting[]): string {
  if (!meetings.length) return ''
  const dates = meetings.map((meeting) => meeting.date).sort()
  const first = new Date(`${dates[0]}T00:00:00Z`)
  const last = new Date(`${dates[dates.length - 1]}T00:00:00Z`)
  if (Number.isNaN(first.valueOf()) || Number.isNaN(last.valueOf())) return `${meetings.length} meetings`
  const month = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' })
  const span = first.getUTCFullYear() === last.getUTCFullYear()
    ? `${month.format(first)}${first.getUTCMonth() === last.getUTCMonth() ? '' : `–${month.format(last)}`} ${last.getUTCFullYear()}`
    : `${month.format(first)} ${first.getUTCFullYear()}–${month.format(last)} ${last.getUTCFullYear()}`
  return `${meetings.length} meetings · ${span}`
}
function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  return <a href={href} target="_blank" rel="noreferrer">{children} <span aria-hidden="true">↗</span></a>
}

function DecisionCard({ bullet, citations, onSeek, selected, onWatch, onSelect }: { bullet: Citation; citations?: InlineCitation[]; onSeek: SeekCitation; selected: boolean; onWatch: (seconds?: number) => void; onSelect: () => void }) {
  const evidence = bullet.evidence
  const votes = evidence?.votes ?? []
  const quotes = (evidence?.quotes ?? []).filter((quote) => quote.text)
  const tally = evidence?.vote_tally
  const outcome = evidence?.outcome ? OUTCOMES[evidence.outcome] : null
  const next = bullet.actions?.find((action) => action.type === 'upcoming_agenda')
  const legislation = bullet.legislation
  const hasDetails = votes.length > 0 || quotes.length > 0 || !!evidence?.topics?.length || !!legislation
  const playable = !!(evidence?.video_mp4 || bullet.clip_id || bullet.url)
  return <article className={`decision-card${selected ? ' is-selected' : ''}`}>
    <div className="decision-meta">{bullet.date && <><time>{dateLabel(bullet.date)}</time><span aria-hidden="true"> · </span></>}Board of Supervisors</div>
    <div className="decision-claim"><AnswerMarkdown markdown={readableText(bullet.text)} citations={citations} onSeek={onSeek} /></div>
    <div className="badges">
      {outcome && <span className="outcome">{outcome}</span>}
      {tally && typeof tally.aye === 'number' && typeof tally.no === 'number' && <span className="vote-pill" aria-label={`${tally.aye} aye, ${tally.no} no`}>{tally.aye}–{tally.no} <span>vote</span></span>}
      {next && <button type="button" className="next-pill" onClick={onSelect}>Up next{next.date ? ` · ${dateLabel(next.date, 'short')}` : ''} <span aria-hidden="true">↗</span></button>}
      {legislation?.fiscal_impact && <span className="vote-pill">Fiscal impact</span>}
    </div>
    {bullet.item_title && <p className="item-title">{readableText(bullet.item_title)}</p>}
    <div className="decision-controls">
      {playable ? <button className="watch-button" type="button" onClick={() => onWatch()}><span aria-hidden="true">▷</span> Watch this moment <span className="watch-time">{timeLabel(startTime(bullet))}</span></button> : <p className="muted">No video link was returned for this item.</p>}
      {evidence?.file_no && <span className="file-number">File {evidence.file_no}</span>}
    </div>
    {hasDetails && <details className="decision-details">
      <summary>Details <span className="detail-hint">{votes.length ? 'Roll call' : 'Legislation'}{quotes.length ? ' & quotes' : ''}</span></summary>
      {votes.length > 0 && <section className="roll-call" aria-label="Roll call">
        <div className="vote-columns">{['aye', 'no'].map((vote) => <div key={vote}>
          <h4>{vote === 'aye' ? 'Aye' : 'No'} <span>{votes.filter((entry) => entry.vote === vote).length}</span></h4>
          <ul>{votes.filter((entry) => entry.vote === vote).map((entry) => <li key={entry.name}>{entry.name}{entry.inferred ? <sup>*</sup> : null}</li>)}</ul>
        </div>)}</div>
        {votes.filter((vote) => !['aye', 'no'].includes(vote.vote)).map((vote) => <p className="muted" key={vote.name}>{vote.name} · {vote.vote}{vote.inferred ? '*' : ''}</p>)}
        {votes.some((vote) => vote.inferred) && <p className="inferred">* Inferred from captions; check the official record.</p>}
      </section>}
      {quotes.length > 0 && <section className="quotes" aria-label="Notable quotes"><h4>In their words</h4>{quotes.map((quote, index) => <div className="quote" key={index}>
        <p>“{readableText(quote.text || '')}”</p>
        {typeof quote.t0 === 'number' && Number.isFinite(quote.t0) ? <button type="button" onClick={() => onWatch(quote.t0!)}>{quote.name || 'Speaker not identified'} <span>▷ {timeLabel(quote.t0)}</span></button> : <span className="muted">{quote.name || 'Speaker not identified'}</span>}
      </div>)}</section>}
      {!!evidence?.topics?.length && <div className="topics" aria-label="Topics">{evidence.topics.map((topic) => <span key={topic}>{topic.replace(/_/g, ' ')}</span>)}</div>}
      {legislation && <LegislationDetails legislation={legislation} />}
    </details>}
  </article>
}

const PHASES: Record<string, string> = { introduced: 'Introduced', in_committee: 'In committee', first_reading: 'First reading', final_passage_scheduled: 'Final passage scheduled', passed: 'Passed', unknown: 'Phase not established' }

function ContactLinks({ contact }: { contact: Contact }) {
  return <div className="contact-links">
    {contact.phone && <a href={`tel:${contact.phone.replace(/[^+\d]/g, '')}`}>{contact.phone}</a>}
    {contact.email && <a href={`mailto:${contact.email}`}>{contact.email}</a>}
  </div>
}

function DocumentLinks({ documents }: { documents?: DocumentLink[] }) {
  if (!documents?.length) return null
  return <ul className="document-links">{documents.map((document, index) => <li key={index}><ExternalLink href={document.url}>{readableText(document.label || 'Official document')}</ExternalLink></li>)}</ul>
}

function LegislationDetails({ legislation }: { legislation: Legislation }) {
  return <section className="legislation-details" aria-label="Legislation history">
    {legislation.phase && <span className="phase-badge">{PHASES[legislation.phase] || legislation.phase.replace(/_/g, ' ')}</span>}
    {!!legislation.sponsors?.length && <div className="sponsors"><h4>Sponsored by</h4><p>{readableText(legislation.sponsors.join(' · '))}</p></div>}
    {!!legislation.history?.length && <><h4>How this item got here</h4><ol className="history">{legislation.history.map((entry, index) => <li key={index}><time>{dateLabel(entry.date, 'short')}</time><span>{entry.source_url ? <ExternalLink href={entry.source_url}>{readableText(entry.action)}</ExternalLink> : readableText(entry.action)}{entry.body && <small>{readableText(entry.body)}</small>}</span></li>)}</ol></>}
    <DocumentLinks documents={legislation.documents} />
    {legislation.source_url && <p className="legislation-source"><ExternalLink href={legislation.source_url}>Official legislation record</ExternalLink></p>}
  </section>
}

function MeetingVideo({ bullet, selection }: { bullet: Citation; selection: Selection }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [fallback, setFallback] = useState(!bullet.evidence?.video_mp4)
  const [failed, setFailed] = useState(false)
  const [playNotice, setPlayNotice] = useState(false)
  const proxy = bullet.clip_id ? `/api/video/${bullet.clip_id}` : null
  const source = fallback ? proxy : bullet.evidence?.video_mp4
  const pending = useRef(true)
  const latest = useRef(selection)
  latest.current = selection

  function seekAndPlay() {
    const video = videoRef.current
    if (!video || video.readyState < 1 || !pending.current) return
    try {
      video.currentTime = latest.current.seconds
      pending.current = false
      if (latest.current.play) {
        void video.play().then(() => setPlayNotice(false)).catch((error: unknown) => {
          if (!(error instanceof DOMException && error.name === 'AbortError')) setPlayNotice(true)
        })
      }
    } catch {
      // Some players expose metadata before the seekable range is ready.
      pending.current = true
    }
  }

  useEffect(() => {
    pending.current = true
    seekAndPlay()
  }, [selection.request, selection.seconds, source])

  function handleError() {
    pending.current = true
    if (!fallback && proxy) setFallback(true)
    else setFailed(true)
  }

  return <section className="video-card" aria-label="Official meeting video">
    <div className="video-frame">
      {source && !failed ? <video ref={videoRef} controls preload="metadata" playsInline src={source} onLoadedMetadata={seekAndPlay} onCanPlay={seekAndPlay} onProgress={seekAndPlay} onError={handleError} aria-label="Board of Supervisors meeting video">
        {bullet.clip_id && <track kind="subtitles" srcLang="en" label="Captions" src={`/api/captions/${bullet.clip_id}.vtt`} />}
      </video> : <p>The video cannot play here right now. {bullet.url ? 'Watch this moment on SFGovTV using the link below.' : 'No official video link was returned.'}</p>}
    </div>
    <p className="video-caption">Board of Supervisors{bullet.date ? ` · ${dateLabel(bullet.date)}` : ''}<br /><span>starts at {timeLabel(selection.seconds)}</span></p>
    {playNotice && <p className="muted" role="status">Your browser paused playback. Press play to watch this moment.</p>}
    {bullet.url && <ExternalLink href={bullet.url}>Open on SFGovTV</ExternalLink>}
    {source && !failed && bullet.clip_id && <SyncedTranscript videoRef={videoRef} startSec={bullet.evidence?.start_sec} endSec={bullet.evidence?.end_sec} quotes={bullet.evidence?.quotes} />}
  </section>
}

function NextSteps({ action }: { action: Action }) {
  if (action.type !== 'upcoming_agenda') return null
  return <section className="next-steps" aria-label="What happens next">
    <h2>What happens next</h2>
    <p className="schedule">This item is scheduled{action.body ? <> at the <strong>{readableText(action.body)}</strong></> : ''}{action.date ? <> on <strong>{dateLabel(action.date, 'weekday')}</strong></> : ''}{action.time ? <> at <strong>{action.time}</strong></> : ''}{action.location ? <>, {readableText(action.location)}</> : ''}.</p>
    {action.file_no && <p className="action-file">Upcoming agenda · File {action.file_no}</p>}
    {action.participation && <><h3>How to have your say</h3><dl className="participation">
      {action.participation.in_person && <div><dt>In person</dt><dd>{readableText(action.participation.in_person)}</dd></div>}
      <div><dt>By phone</dt><dd>{readableText(action.participation.remote ?? action.participation.remote_reason ?? (action.participation.remote === null ? (action.body?.toLowerCase().includes('committee') ? 'Not offered at committee meetings.' : 'Remote participation is not offered for this meeting.') : 'Phone participation details were not provided.'))}</dd></div>
      {(action.participation.written || action.participation.written_comment) && <div><dt>In writing</dt><dd>{action.participation.written_comment ? <>
        {action.participation.written_comment.deadline && <p>Submit {readableText(action.participation.written_comment.deadline)}.</p>}
        <ContactLinks contact={{ email: action.participation.written_comment.committee_clerk_email || action.participation.written_comment.email }} />
        {action.participation.written_comment.mailing_address && <p>{readableText(action.participation.written_comment.mailing_address)}</p>}
      </> : readableText(action.participation.written || '')}</dd></div>}
    </dl></>}
    <div className="action-links">{action.source_url && <ExternalLink href={action.source_url}>Meeting page</ExternalLink>}{action.agenda_url && <ExternalLink href={action.agenda_url}>Agenda PDF</ExternalLink>}</div>
    <p className="action-note">Check the published agenda for comment rules and schedule changes.</p>
  </section>
}

function AdditionalAction({ action, legislation }: { action: Action; legislation?: Legislation | null }) {
  let content: React.ReactNode
  switch (action.type) {
    case 'interpretation_or_ada_request':
      content = <><h2>Need interpretation or accessibility help?</h2>
        {action.deadline && <p>{readableText(action.deadline)}</p>}
        {!!action.languages?.length && <p>{action.languages.join(' · ')}</p>}
        <ContactLinks contact={action} />
        {action.tty && <p>TTY: <a href={`tel:${action.tty.replace(/[^+\d]/g, '')}`}>{action.tty}</a></p>}
      </>
      break
    case 'contact_sponsor':
      content = <><h2>Sponsored by {action.profile_url ? <ExternalLink href={action.profile_url}>{action.name || 'a supervisor'}</ExternalLink> : action.name || 'a supervisor'}</h2>
        {action.district != null && <p>District {action.district}</p>}<ContactLinks contact={action} />
      </>
      break
    case 'contact_committee_clerk': {
      const committee = legislation?.committee
      content = <><h2>This item is at the {action.committee || committee?.name || 'committee'}</h2>
        {!!committee?.current_members?.length && <ul className="committee-members">{committee.current_members.map((member) => <li key={member.name}>{member.name}{member.role ? <span>{member.role}</span> : null}</li>)}</ul>}
        {legislation?.your_supervisor_on_committee != null && <p className="committee-match">{legislation.your_supervisor_on_committee ? 'Your supervisor serves on this committee.' : 'Your supervisor does not serve on this committee.'}</p>}
        {(action.name || committee?.clerk?.name) && <p className="clerk-name">Committee clerk · {action.name || committee?.clerk?.name}</p>}
        <ContactLinks contact={action.email || action.phone ? action : committee?.clerk || {}} />
      </>
      break
    }
    case 'mayor_review':
      content = <><h2>Passed{action.final_passage_date ? ` ${dateLabel(action.final_passage_date, 'short')}` : ''}. Now with the Mayor.</h2>
        <p>Under city rules the Mayor has 10 days after receiving an ordinance to sign it, return it unsigned, or veto it.</p>
      </>
      break
    case 'election_measure':
      content = <><h2>{action.letter ? `Measure ${action.letter}${action.title ? ' · ' : ''}` : ''}{readableText(action.title || (!action.letter ? 'Election measure' : ''))}</h2>
        {action.election_date && <p>Election day · <strong>{dateLabel(action.election_date)}</strong></p>}
        <DocumentLinks documents={action.documents} />
      </>
      break
    default:
      return null
  }
  const linkLabel = action.type === 'mayor_review' ? "How the Mayor's review works"
    : action.type === 'interpretation_or_ada_request' ? 'Accessibility and interpretation services'
      : action.type === 'contact_sponsor' ? 'Supervisor profile'
        : action.type === 'contact_committee_clerk' ? 'Committee page' : 'Election information'
  return <section className="additional-action">{content}{action.source_url && <p className="action-source"><ExternalLink href={action.source_url}>{linkLabel}</ExternalLink></p>}</section>
}

function SupervisorCard({ supervisor, fallback }: { supervisor: Supervisor; fallback: boolean }) {
  return <section className="supervisor-card" aria-label="Your supervisor">
    <h2>{supervisor.profile_url ? <ExternalLink href={supervisor.profile_url}>{supervisor.name}</ExternalLink> : supervisor.name}</h2>
    {supervisor.district != null && <p>District {supervisor.district}</p>}
    <div className="supervisor-contact">{supervisor.phone && <a href={`tel:${supervisor.phone.replace(/[^+\d]/g, '')}`}>{supervisor.phone}</a>}{supervisor.email && <a href={`mailto:${supervisor.email}`}>{supervisor.email}</a>}</div>
    {fallback && <p className="muted">City Hall is the fallback location, not your home address.</p>}
  </section>
}

function rowText(row: Record<string, unknown>, names: string[]): string | null {
  for (const name of names) if (typeof row[name] === 'string' && row[name]) return readableText(row[name] as string)
  return null
}

function Method({ result, scope }: { result: AskResponse; scope: string }) {
  if (result.retrieval.mode === 'mock') return null
  const rows = result.retrieval.rows ?? []
  return <footer className="method">
    <p>Answered from the official meeting record{scope ? ` · ${scope}` : ''} · {rows.length} {rows.length === 1 ? 'record' : 'records'} matched{typeof result.latency_ms === 'number' ? ` · ${Math.round(result.latency_ms / 1000)} s` : ''}</p>
    <details><summary>See how this was found</summary><div className="method-panel">
      <h2>From your question to the record</h2>
      <p className="method-steps">Gemini read your question <span>→</span> {result.retrieval.mode === 'summaries' ? 'the meeting summaries were retrieved' : 'chose a graph query'} <span>→</span> FalkorDB returned {rows.length} {rows.length === 1 ? 'record' : 'records'} <span>→</span> Gemini wrote the answer from only those records.</p>
      {result.retrieval.cypher && <><h3>The graph query</h3><pre><code>{result.retrieval.cypher}</code></pre></>}
      <h3>Matched records</h3>
      {rows.length ? <ul className="matched-records">{rows.map((row, index) => {
        const title = rowText(row, ['item_title', 'title', 'i.title', 'name', 's.name'])
        const date = rowText(row, ['date', 'm.date'])
        const outcome = rowText(row, ['outcome', 'i.outcome'])
        return <li key={index}>{title || 'Meeting record'}{date ? ` · ${dateLabel(date)}` : ''}{outcome ? ` · ${OUTCOMES[outcome] || outcome.replace(/_/g, ' ')}` : ''}</li>
      })}</ul> : <p className="muted">No matching records were returned.</p>}
      <p className="method-note">AI can make mistakes. Use the linked meeting video and official agenda to check an answer.</p>
    </div></details>
  </footer>
}

type Turn = { question: string; response: AskResponse; scope: string; location: Location | null }
const FOLLOW_UPS = ['Who voted against it?', 'What happens next?', 'What did the public say?']

function App() {
  const [question, setQuestion] = useState('')
  const [pendingQuestion, setPendingQuestion] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [selectedTurn, setSelectedTurn] = useState(0)
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [selectedMeetingIds, setSelectedMeetingIds] = useState<number[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [streaming, setStreaming] = useState<{ answer: string; citations: InlineCitation[]; steps: RetrievalStep[] }>({ answer: '', citations: [], steps: [] })
  const [streamSource, setStreamSource] = useState<Citation | null>(null)
  const [error, setError] = useState('')
  const [meetingsError, setMeetingsError] = useState('')
  const [location, setLocation] = useState<Location | null>(null)
  const [locating, setLocating] = useState(false)
  const [selection, setSelection] = useState<Selection>({ index: 0, seconds: 0, play: false, request: 0 })
  const sidebarRef = useRef<HTMLElement>(null)
  const requestRef = useRef<AbortController | null>(null)
  const locationRequest = useRef(0)
  const turn = turns[selectedTurn]
  const result = turn?.response
  const active = streamSource ?? result?.bullets[selection.index]
  const empty = turns.length === 0 && !pendingQuestion
  const followUps = turns[turns.length - 1]?.response.suggestions ?? FOLLOW_UPS
  const stepLabels = streaming.steps.map((step) => {
    if (step.note) return 'Checking available evidence'
    if (step.tool === 'search_items' || step.tool === 'run_cypher') return `Searching agenda items → ${step.rows} found`
    if (step.tool === 'run_template' || step.tool === 'meeting_overview') return `Checking meeting records → ${step.rows} found`
    if (step.tool === 'get_item') return 'Reading item'
    if (step.tool === 'legislation') return 'Checking legislation'
    if (step.tool === 'next_steps') return 'Checking what happens next'
    return 'Writing answer'
  }).filter((label, index, labels) => index === 0 || label !== labels[index - 1]).slice(-4)

  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/meetings', { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error()
      const payload: unknown = await response.json()
      if (!Array.isArray(payload) || !payload.every((meeting) => meeting && typeof meeting.clip_id === 'number' && typeof meeting.date === 'string')) throw new Error()
      setMeetings(payload)
    }).catch(() => {
      if (!controller.signal.aborted) setMeetingsError('The meeting list is unavailable right now. You can still ask a question across the record.')
    })
    return () => { controller.abort(); requestRef.current?.abort(); locationRequest.current += 1 }
  }, [])

  useEffect(() => {
    if (turns.length) document.getElementById(`turn-${turns.length - 1}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [turns.length])

  useEffect(() => {
    if (pendingQuestion) document.querySelector('.pending-chat-turn')?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [pendingQuestion])

  async function ask(nextQuestion: string) {
    const text = nextQuestion.trim()
    if (!text || isLoading) return
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setQuestion('')
    setPendingQuestion(text)
    setIsLoading(true)
    setError('')
    setStreaming({ answer: '', citations: [], steps: [] })
    setStreamSource(null)
    const history = turns.slice(-3).flatMap((previous) => [
      { role: 'user', content: previous.question, cited: [] },
      { role: 'assistant', content: [previous.response.answer, ...previous.response.bullets.map((bullet) => bullet.text)].join('\n\n'), cited: previous.response.bullets.filter((bullet) => bullet.clip_id != null && bullet.meta_id != null).map((bullet) => ({ clip_id: bullet.clip_id, meta_id: bullet.meta_id, item_title: bullet.item_title || '' })) },
    ])
    try {
      let response: AskResponse
      if (USE_MOCK) {
        response = fixtureFor(text)
      } else {
        const payload = await askStream<unknown>({
          question: text, clip_ids: selectedMeetingIds.length ? selectedMeetingIds : null, history,
          ...(location ? { lat: location.lat, lon: location.lon } : {}),
        }, {
          signal: controller.signal,
          onStep: (step) => setStreaming((current) => ({ ...current, steps: [...current.steps, step] })),
          onDelta: (delta) => setStreaming((current) => {
            const citations = new Map(current.citations.map((citation) => [citation.key, citation]))
            for (const citation of delta.citations ?? []) citations.set(citation.key, citation)
            return { ...current, answer: current.answer + delta.text, citations: [...citations.values()] }
          }),
        })
        if (!isAskResponse(payload)) throw new Error('The meeting record returned an incomplete answer. Please try again.')
        response = payload
      }
      if (controller.signal.aborted) return
      const scope = selectedMeetingIds.length ? `Limited to ${selectedMeetingIds.length} ${selectedMeetingIds.length === 1 ? 'meeting' : 'meetings'}, ${getMeetingRangeLabel(meetings, selectedMeetingIds)}` : (meetings.length ? `${meetings.length} meetings available` : '')
      setTurns((previous) => [...previous, { question: text, response, scope, location }])
      setSelectedTurn(turns.length)
      setSelection((current) => ({ index: 0, seconds: response.bullets[0] ? startTime(response.bullets[0]) : 0, play: false, request: current.request + 1 }))
      setPendingQuestion('')
      setStreamSource(null)
    } catch (caught) {
      if (!controller.signal.aborted) setError(caught instanceof Error && caught.message.startsWith('The meeting record') ? caught.message : 'We couldn’t read the meeting record just now. Please try your question again.')
    } finally {
      if (!controller.signal.aborted) setIsLoading(false)
    }
  }

  function findSupervisor() {
    const request = ++locationRequest.current
    setLocating(true)
    const done = (value: Location) => {
      if (request !== locationRequest.current) return
      setLocation(value)
      setLocating(false)
    }
    if (!navigator.geolocation) { done(CITY_HALL); return }
    navigator.geolocation.getCurrentPosition(
      (position) => done({ lat: position.coords.latitude, lon: position.coords.longitude, fallback: false }),
      () => done(CITY_HALL),
      { timeout: 6000, maximumAge: 300000 },
    )
  }

  function seekCitation(turnIndex: number, clipId: number, metaId: number, seconds?: number) {
    const response = turns[turnIndex].response
    let index = response.bullets.findIndex((bullet) => bullet.clip_id === clipId && bullet.meta_id === metaId)
    if (index < 0) {
      const citation = response.citations?.find((entry) => entry.clip_id === clipId && entry.meta_id === metaId)
      if (!citation) return
      index = response.bullets.length
      const row = response.retrieval.rows?.find((entry) => entry.clip_id === clipId && entry.meta_id === metaId)
      const bullet: Citation = { ...citation, text: typeof row?.summary === 'string' ? row.summary : citation.item_title || 'Meeting evidence' }
      setTurns((previous) => previous.map((turn, i) => i === turnIndex ? { ...turn, response: { ...turn.response, bullets: [...turn.response.bullets, bullet] } } : turn))
    }
    select(turnIndex, index, true, seconds)
  }

  function select(turnIndex: number, index: number, play: boolean, seconds?: number) {
    const bullet = turns[turnIndex]?.response.bullets[index]
    setStreamSource(null)
    setSelectedTurn(turnIndex)
    setSelection((current) => ({ index, seconds: seconds ?? (bullet ? startTime(bullet) : 0), play, request: current.request + 1 }))
    requestAnimationFrame(() => {
      if (sidebarRef.current) sidebarRef.current.scrollTop = 0
      if (window.matchMedia('(max-width: 900px)').matches) {
        const target = play ? sidebarRef.current?.querySelector('.video-card') : sidebarRef.current
        target?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    })
  }

  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); void ask(question) }
  const composer = <section className={`question-area${empty ? '' : ' composer-dock'}`} aria-label="Ask the meeting record">
    {!empty && !!turns.length && <div className="follow-up">{followUps.map((followUp) => <button key={followUp} type="button" disabled={isLoading} onClick={() => void ask(followUp)}>{readableText(followUp)}</button>)}</div>}
    <form className="ask-form" onSubmit={submit}><label className="sr-only" htmlFor="question">Ask about a decision, person, or policy</label><input id="question" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={empty ? 'What would you like to know about your city?' : 'Ask a follow-up, or a new question about your city'} disabled={isLoading} /><button type="submit" disabled={isLoading || !question.trim()}>{isLoading ? 'Reading…' : 'Ask the record'}</button></form>
    <div className="search-options"><MeetingFilter meetings={meetings} selectedIds={selectedMeetingIds} onChange={setSelectedMeetingIds} disabled={isLoading} /><button className="location-button" type="button" onClick={findSupervisor} disabled={locating || isLoading}>{locating ? 'Finding your district…' : location ? 'Update my location' : 'Find my supervisor'} <span aria-hidden="true">↗</span></button></div>
    {location && <p className="location-notice" role="status">{location.fallback ? 'Location unavailable. Using City Hall, not your home.' : 'Your location is ready.'} {turns[turns.length - 1]?.location === location ? 'District information is included when available.' : 'Ask a question to include the supervisor for this location.'}</p>}
    {meetingsError && <p className="muted" role="status">{meetingsError}</p>}
  </section>

  return <div className="app-shell">
    <header className="topbar"><a className="brand" href="/" aria-label="Board of SuperVision home"><img src="/logo.svg" width="48" height="48" alt="" /><span className="brand-wordmark">Board of <span>Super<span className="vision-accent">Vision</span></span></span></a></header>
    <main className={empty ? 'main empty-main' : 'main answer-main'}>
      {empty ? <>
        <section className="hero"><h1><img src="/logo.svg" width="40" height="40" alt="" /><span>Board of Super<span className="vision-accent">Vision</span></span></h1><p className="hero-copy">Ask anything that happened at a San Francisco Board of Supervisors meeting. Then see what happens next and how to weigh in.</p><ExampleLookup onPick={(example) => { setQuestion(example); document.getElementById('question')?.focus() }} /></section>
        {composer}
        <section className="suggestions-section"><h2>A few places to start</h2><div className="suggestions">{SUGGESTIONS.map((suggestion) => <button className="suggestion-card" type="button" key={suggestion.label} disabled={isLoading} onClick={() => void ask(suggestion.question)}><span className="suggestion-label">{suggestion.label}</span><span className="suggestion-question">{suggestion.question}</span><span className="suggestion-arrow" aria-hidden="true">↗</span></button>)}</div></section><p className="corpus">{selectedMeetingIds.length ? `Answering from ${selectedMeetingIds.length} ${selectedMeetingIds.length === 1 ? 'meeting' : 'meetings'}, ${getMeetingRangeLabel(meetings, selectedMeetingIds)}` : corpusLabel(meetings) || (meetingsError ? 'Meeting coverage unavailable' : 'Loading the meeting record…')}<span>Answers linked to official meeting video</span></p>
      </> : <>
        <h1 className="sr-only">Your conversation with the meeting record</h1>
        <div className="chat-workspace">
          <section className="conversation" aria-label="Conversation">
            {turns.map((item, turnIndex) => <article className={`chat-turn${turnIndex === selectedTurn ? ' selected-turn' : ''}`} key={turnIndex} id={`turn-${turnIndex}`}>
              <div className="user-turn"><span>You asked</span><h2><button type="button" aria-pressed={turnIndex === selectedTurn} onClick={() => select(turnIndex, 0, false)}>{readableText(item.question)}</button></h2></div>
              <section className="assistant-turn" aria-label={`Answer to ${item.question}`}>
                <div className="answer-copy"><AnswerMarkdown markdown={readableText(item.response.answer || 'The record did not return an answer to this question. Try asking about a specific decision or meeting.')} citations={item.response.citations} onSeek={(clipId, metaId, seconds) => seekCitation(turnIndex, clipId, metaId, seconds)} /></div>
                <section className="decisions" aria-label="Decisions and evidence"><h3 className="section-heading">The decisions behind the answer <span>{item.response.bullets.length}</span></h3>{item.response.bullets.map((bullet, index) => <DecisionCard key={index} bullet={bullet} citations={item.response.citations} onSeek={(clipId, metaId, seconds) => seekCitation(turnIndex, clipId, metaId, seconds)} selected={turnIndex === selectedTurn && selection.index === index} onWatch={(seconds) => select(turnIndex, index, true, seconds)} onSelect={() => select(turnIndex, index, false)} />)}{!item.response.bullets.length && <p className="muted">No item-level citations were returned for this answer.</p>}</section>
                <Method result={item.response} scope={item.scope} />
              </section>
            </article>)}
            {pendingQuestion && <article className="chat-turn pending-chat-turn">{!isLoading && <div className="user-turn"><span>You asked</span><h2>{readableText(pendingQuestion)}</h2></div>}<div className="pending-answer">
              {isLoading ? <><p className="loading-line stream-steps" role="status"><span />{stepLabels.join(' · ') || 'Reading the meeting record…'}</p>{streaming.answer && <div className="answer-copy streaming-answer" aria-busy="true"><AnswerMarkdown markdown={readableText(streaming.answer)} citations={streaming.citations} onSeek={(clipId, metaId, seconds) => { const citation = streaming.citations.find((entry) => entry.clip_id === clipId && entry.meta_id === metaId); if (citation) { setStreamSource({ ...citation, text: citation.item_title || 'Meeting evidence' }); setSelection((current) => ({ ...current, seconds: seconds ?? citation.t0 ?? 0, play: true, request: current.request + 1 })) } }} /></div>}</> : error && <><p className="error-banner" role="alert">{error}</p><button type="button" className="retry-button" onClick={() => void ask(pendingQuestion)}>Try again</button></>}
            </div></article>}
          </section>
          <aside className="answer-sidebar" ref={sidebarRef} aria-label="Watch and take part">
            {active?.actions?.filter((action) => action.type === 'upcoming_agenda').map((action, index) => <NextSteps key={`${selectedTurn}-${selection.index}-${index}`} action={action} />)}
            {active && <MeetingVideo key={`${active.clip_id}-${active.evidence?.video_mp4}`} bullet={active} selection={selection} />}
            {result && !active && <p className="muted">This answer has no item-level video citations. Select an earlier answer to revisit its sources.</p>}
            {result?.supervisor && <SupervisorCard supervisor={result.supervisor} fallback={turn?.location?.fallback ?? false} />}
            {active?.actions?.map((action, index) => <AdditionalAction key={`${selectedTurn}-${selection.index}-${index}`} action={action} legislation={active.legislation} />)}
            {turn?.location && !result?.supervisor && <p className="location-notice">No supervisor was returned for this location. Try updating your location and asking again.</p>}
          </aside>
        </div>
        {composer}
      </>}
      {USE_MOCK && <p className="mock-notice">Demo mode · Answers use saved fixtures, not a live search.</p>}
    </main>
    <footer className="site-footer"><span>An independent project · Not an official City service</span></footer>
  </div>
}

export default App
