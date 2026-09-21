import { useEffect, useMemo, useRef, useState } from 'react'
import type { RefObject } from 'react'
import './SyncedTranscript.css'

type SyncedTranscriptProps = {
  videoRef: RefObject<HTMLVideoElement | null>
  startSec?: number | null
  endSec?: number | null
  quotes?: { t0?: number | null }[] | null
}

type TranscriptCue = {
  start: number
  end: number
  text: string
  timestamp: string
}

const initializedTracks = new WeakSet<TextTrack>()
const supervisorNames = [
  'Connie Chan', 'Stephen Sherrill', 'Danny Sauter', 'Alan Wong',
  'Bilal Mahmood', 'Matt Dorsey', 'Myrna Melgar', 'Rafael Mandelman',
  'Jackie Fielder', 'Shamann Walton', 'Chyanne Chen',
  'Chan', 'Chen', 'Sherrill', 'Sauter', 'Wong', 'Mahmood',
  'Dorsey', 'Melgar', 'Mandelman', 'Fielder', 'Walton',
]
const supervisorPattern = new RegExp(`\\b(${supervisorNames.join('|')})\\b`, 'gi')
const canonicalNames: Record<string, string> = Object.fromEntries(supervisorNames.map(name => [name.toLowerCase(), name]))

function displayText(cue: TextTrackCue): string {
  const textCue = cue as VTTCue
  let text = (typeof textCue.getCueAsHTML === 'function'
    ? textCue.getCueAsHTML().textContent ?? ''
    : textCue.text ?? '').replace(/\u2014/g, ', ').replace(/\s+/g, ' ').trim()
  if (/[A-Z]/.test(text) && !/[a-z]/.test(text)) {
    text = text.toLowerCase().replace(/(^|[.!?]\s+)([a-z])/g, (_, prefix: string, letter: string) => prefix + letter.toUpperCase())
    // Caption speaker markers can precede the first sentence.
    text = text.replace(/^(>+\s*)([a-z])/, (_, prefix: string, letter: string) => prefix + letter.toUpperCase())
    text = text.replace(supervisorPattern, name => canonicalNames[name.toLowerCase()] ?? name)
  }
  return text
}

function timestamp(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  return `${String(Math.floor(whole / 60)).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}`
}

function finiteTime(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

export default function SyncedTranscript({ videoRef, startSec, endSec, quotes }: SyncedTranscriptProps) {
  const [allCues, setAllCues] = useState<TranscriptCue[]>([])
  const [duration, setDuration] = useState<number>(Infinity)
  const [status, setStatus] = useState<'loading' | 'ready' | 'unavailable'>('loading')
  const [activeIndex, setActiveIndex] = useState(-1)
  const [following, setFollowing] = useState(true)
  const [seekError, setSeekError] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLButtonElement>(null)
  const resumeTimer = useRef<number | undefined>(undefined)
  const scrollTimer = useRef<number | undefined>(undefined)
  const programmaticScroll = useRef(false)

  useEffect(() => {
    const video = videoRef.current
    if (!video) {
      setStatus('unavailable')
      return
    }
    let selectedTrack: TextTrack | null = null
    let loadedCues: TextTrackCueList | null = null
    let loadedLength = -1
    const trackElements = new Set<HTMLTrackElement>()
    const tracks = video.textTracks
    const timeout = setTimeout(() => {
      if (!loadedCues?.length) setStatus('unavailable')
    }, 12000)

    function readCues() {
      if (Number.isFinite(video!.duration)) setDuration(video!.duration)
      const cues = selectedTrack?.cues
      // Turning native captions off must not discard the transcript already read.
      if (!cues?.length || (cues === loadedCues && cues.length === loadedLength)) return
      loadedCues = cues
      loadedLength = cues.length
      const next = Array.from(cues, cue => ({
        start: cue.startTime,
        end: cue.endTime,
        text: displayText(cue),
        timestamp: timestamp(cue.startTime),
      })).sort((a, b) => a.start - b.start)
      setAllCues(next)
      setStatus('ready')
      clearTimeout(timeout)
    }

    function trackError() {
      if (!loadedCues?.length) setStatus('unavailable')
    }

    function connectTracks() {
      for (const element of video!.querySelectorAll('track')) {
        if (trackElements.has(element)) continue
        trackElements.add(element)
        element.addEventListener('load', readCues)
        element.addEventListener('error', trackError)
      }
      const nextTrack = Array.from(tracks).find(track => track.kind === 'subtitles' || track.kind === 'captions') ?? tracks[0] ?? null
      if (nextTrack !== selectedTrack) {
        selectedTrack?.removeEventListener('cuechange', readCues)
        selectedTrack = nextTrack
        selectedTrack?.addEventListener('cuechange', readCues)
        loadedCues = null
        loadedLength = -1
      }
      if (selectedTrack && !initializedTracks.has(selectedTrack)) {
        initializedTracks.add(selectedTrack)
        if (selectedTrack.mode === 'disabled') selectedTrack.mode = 'hidden'
      }
      readCues()
      if (!selectedTrack && video!.readyState >= 1) setStatus('unavailable')
    }

    function checkPendingCues() {
      if (!loadedCues?.length) readCues()
    }

    function clearPlaybackNotice() {
      setSeekError(false)
    }

    connectTracks()
    video.addEventListener('loadedmetadata', connectTracks)
    video.addEventListener('loadeddata', connectTracks)
    video.addEventListener('durationchange', readCues)
    video.addEventListener('timeupdate', checkPendingCues)
    video.addEventListener('playing', clearPlaybackNotice)
    tracks.addEventListener('addtrack', connectTracks)
    tracks.addEventListener('change', readCues)
    return () => {
      clearTimeout(timeout)
      video.removeEventListener('loadedmetadata', connectTracks)
      video.removeEventListener('loadeddata', connectTracks)
      video.removeEventListener('durationchange', readCues)
      video.removeEventListener('timeupdate', checkPendingCues)
      video.removeEventListener('playing', clearPlaybackNotice)
      tracks.removeEventListener('addtrack', connectTracks)
      tracks.removeEventListener('change', readCues)
      selectedTrack?.removeEventListener('cuechange', readCues)
      for (const element of trackElements) {
        element.removeEventListener('load', readCues)
        element.removeEventListener('error', trackError)
      }
    }
  }, [videoRef])

  const cues = useMemo(() => {
    const from = Math.max(0, finiteTime(startSec) ? startSec - 30 : 0)
    const to = Math.min(duration, finiteTime(endSec) ? endSec + 30 : duration)
    const quoteTimes = (quotes ?? []).map(quote => quote.t0).filter(finiteTime).sort((a, b) => a - b)
    let latestEnd = -Infinity
    return allCues.filter(cue => cue.end > from && cue.start < to).map(cue => {
      latestEnd = Math.max(latestEnd, cue.end)
      let low = 0
      let high = quoteTimes.length
      while (low < high) {
        const mid = (low + high) >>> 1
        if (quoteTimes[mid] < cue.start) low = mid + 1
        else high = mid
      }
      return { ...cue, latestEnd, quoted: low < quoteTimes.length && quoteTimes[low] < cue.end }
    })
  }, [allCues, duration, startSec, endSec, quotes])

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    function updateActive() {
      const time = video!.currentTime
      let low = 0
      let high = cues.length
      while (low < high) {
        const mid = (low + high) >>> 1
        if (cues[mid].start <= time) low = mid + 1
        else high = mid
      }
      let index = low - 1
      // Usually cues do not overlap; the prefix maximum also handles overlapping captions.
      while (index >= 0 && cues[index].end <= time && cues[index].latestEnd > time) index -= 1
      setActiveIndex(index >= 0 && cues[index].end > time ? index : -1)
    }
    updateActive()
    video.addEventListener('timeupdate', updateActive)
    video.addEventListener('seeking', updateActive)
    return () => {
      video.removeEventListener('timeupdate', updateActive)
      video.removeEventListener('seeking', updateActive)
    }
  }, [videoRef, cues])

  function finishProgrammaticScroll() {
    window.clearTimeout(scrollTimer.current)
    scrollTimer.current = window.setTimeout(() => { programmaticScroll.current = false }, 180)
  }

  useEffect(() => {
    const container = scrollRef.current
    const active = activeRef.current
    if (!following || activeIndex < 0 || !container || !active) return
    const top = container.scrollTop + active.getBoundingClientRect().top - container.getBoundingClientRect().top - container.clientTop - (container.clientHeight - active.offsetHeight) / 2
    const target = Math.max(0, Math.min(top, container.scrollHeight - container.clientHeight))
    if (Math.abs(container.scrollTop - target) < 1) return
    programmaticScroll.current = true
    container.scrollTo({ top: target, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
    finishProgrammaticScroll()
  }, [activeIndex, following, cues])

  useEffect(() => () => {
    window.clearTimeout(resumeTimer.current)
    window.clearTimeout(scrollTimer.current)
  }, [])

  function pauseFollow() {
    if (programmaticScroll.current) {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollTop, behavior: 'auto' })
    }
    programmaticScroll.current = false
    window.clearTimeout(scrollTimer.current)
    setFollowing(false)
    window.clearTimeout(resumeTimer.current)
    resumeTimer.current = window.setTimeout(() => setFollowing(true), 4000)
  }

  function resumeFollow() {
    window.clearTimeout(resumeTimer.current)
    setFollowing(true)
  }

  function seekTo(seconds: number) {
    const video = videoRef.current
    if (!video) return
    try {
      video.currentTime = seconds
      setSeekError(false)
      resumeFollow()
      void video.play().catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) setSeekError(true)
      })
    } catch {
      setSeekError(true)
    }
  }

  return <section className="synced-transcript" aria-label="Meeting transcript">
    <div className="synced-transcript-header">
      <h3>Meeting transcript</h3>
      <button type="button" className="transcript-follow" onClick={resumeFollow} disabled={following} aria-label={following ? 'Following video playback' : 'Resume following video playback'}>
        {following ? 'Following' : 'Follow'}
      </button>
    </div>
    <div className="synced-transcript-scroll" ref={scrollRef} tabIndex={0} role="region" aria-label="Transcript lines. Select a line to play that moment."
      onWheel={pauseFollow}
      onTouchStart={pauseFollow}
      onTouchMove={pauseFollow}
      onPointerDown={event => { if (event.target === event.currentTarget) pauseFollow() }}
      onKeyDown={event => {
        if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key) || (event.key === ' ' && event.target === event.currentTarget)) pauseFollow()
      }}
      onScroll={() => {
        if (programmaticScroll.current) finishProgrammaticScroll()
        else pauseFollow()
      }}>
      {status !== 'ready' ? <p className="transcript-notice" role="status">{status === 'loading' ? 'Loading the meeting transcript…' : 'The transcript is not available for this meeting.'}</p>
        : cues.length === 0 ? <p className="transcript-notice">No transcript lines are available for this agenda window.</p>
          : <ol className="transcript-lines">
            {cues.map((cue, index) => <li key={`${cue.start}-${cue.end}-${index}`}>
              <button type="button" className={`transcript-cue${index === activeIndex ? ' is-current' : ''}${cue.quoted ? ' is-quoted' : ''}`}
                ref={index === activeIndex ? activeRef : undefined}
                aria-current={index === activeIndex ? 'true' : undefined}
                aria-label={`Play at ${cue.timestamp}${cue.quoted ? ', quoted moment' : ''}: ${cue.text}`}
                onClick={() => seekTo(cue.start)}>
                <span className="transcript-timestamp" aria-hidden="true">{cue.timestamp}</span>
                <span className="transcript-cue-text">{cue.text}</span>
                {index === activeIndex && <span className="transcript-current-indicator" aria-hidden="true" />}
              </button>
            </li>)}
          </ol>}
    </div>
    {seekError && <p className="transcript-play-notice" role="status">Use the video’s play control to continue from this moment.</p>}
  </section>
}
