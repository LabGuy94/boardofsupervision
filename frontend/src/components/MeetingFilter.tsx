import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import './MeetingFilter.css'

type Meeting = { clip_id: number; date: string; n_items: number }
type MeetingFilterProps = {
  meetings: Meeting[]
  selectedIds: number[]
  onChange: (ids: number[]) => void
  disabled?: boolean
}

const shortDate = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
const fullDate = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
const monthDate = new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' })
const dayKey = (date: string) => date.slice(0, 10)
const asDate = (date: string) => new Date(`${dayKey(date)}T00:00:00Z`)

export function getMeetingRangeLabel(meetings: Meeting[], selectedIds: number[]): string {
  if (!selectedIds.length) return ''
  const ids = new Set(selectedIds)
  const dates = meetings.filter(meeting => ids.has(meeting.clip_id)).map(meeting => dayKey(meeting.date)).sort()
  if (!dates.length) return ''
  const first = dates[0]
  const last = dates[dates.length - 1]
  if (first === last) return fullDate.format(asDate(first))
  const start = first.slice(0, 4) === last.slice(0, 4) ? shortDate.format(asDate(first)) : fullDate.format(asDate(first))
  return `${start} to ${fullDate.format(asDate(last))}`
}

export default function MeetingFilter({ meetings, selectedIds, onChange, disabled = false }: MeetingFilterProps) {
  const [open, setOpen] = useState(false)
  const [focusedId, setFocusedId] = useState<number | null>(null)
  const [position, setPosition] = useState({ top: 0, left: 0, maxHeight: 520 })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const anchorRef = useRef<string | null>(null)
  const awaitingEndRef = useRef(false)
  const dialogId = useId()
  const ordered = useMemo(() => [...meetings].sort((a, b) => dayKey(a.date).localeCompare(dayKey(b.date)) || a.clip_id - b.clip_id), [meetings])
  const selected = useMemo(() => new Set(selectedIds), [selectedIds])
  const selectedMeetings = ordered.filter(meeting => selected.has(meeting.clip_id))
  const firstDate = ordered.length ? dayKey(ordered[0].date) : ''
  const latestDate = ordered.length ? dayKey(ordered[ordered.length - 1].date) : ''
  const fromDate = selectedMeetings.length ? dayKey(selectedMeetings[0].date) : firstDate
  const toDate = selectedMeetings.length ? dayKey(selectedMeetings[selectedMeetings.length - 1].date) : latestDate
  const rangeLabel = getMeetingRangeLabel(ordered, selectedIds)
  const triggerLabel = rangeLabel ? `${rangeLabel} · ${selectedMeetings.length} ${selectedMeetings.length === 1 ? 'meeting' : 'meetings'}` : 'All meetings'
  const unavailable = disabled || !ordered.length
  const visible = open && !unavailable
  const groups = useMemo(() => {
    const result: { month: string; meetings: Meeting[] }[] = []
    for (let index = ordered.length - 1; index >= 0; index--) {
      const meeting = ordered[index]
      const month = dayKey(meeting.date).slice(0, 7)
      const last = result[result.length - 1]
      if (last?.month === month) last.meetings.push(meeting)
      else result.push({ month, meetings: [meeting] })
    }
    return result
  }, [ordered])

  useEffect(() => {
    if (unavailable) setOpen(false)
  }, [unavailable])

  useLayoutEffect(() => {
    if (!visible) return
    const placePanel = () => {
      const trigger = triggerRef.current
      if (!trigger) return
      const rect = trigger.getBoundingClientRect()
      const viewport = window.visualViewport
      const viewportTop = viewport?.offsetTop ?? 0
      const viewportLeft = viewport?.offsetLeft ?? 0
      const height = viewport?.height ?? window.innerHeight
      const width = viewport?.width ?? window.innerWidth
      const gap = 8
      const below = viewportTop + height - rect.bottom - gap * 2
      const above = rect.top - viewportTop - gap * 2
      const opensAbove = below < 360 && above > below
      const maxHeight = Math.max(0, Math.min(520, opensAbove ? above : below))
      const panelHeight = Math.min(panelRef.current?.scrollHeight ?? maxHeight, maxHeight)
      setPosition({
        top: opensAbove ? rect.top - gap - panelHeight : rect.bottom + gap,
        left: Math.max(viewportLeft + gap, Math.min(rect.left, viewportLeft + width - Math.min(360, width - gap * 2) - gap)),
        maxHeight,
      })
    }
    placePanel()
    const firstOption = panelRef.current?.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"], [role="option"][tabindex="0"]')
    firstOption?.focus({ preventScroll: true })
    firstOption?.scrollIntoView({ block: 'nearest' })
    window.addEventListener('resize', placePanel)
    window.addEventListener('scroll', placePanel, true)
    window.visualViewport?.addEventListener('resize', placePanel)
    window.visualViewport?.addEventListener('scroll', placePanel)
    return () => {
      window.removeEventListener('resize', placePanel)
      window.removeEventListener('scroll', placePanel, true)
      window.visualViewport?.removeEventListener('resize', placePanel)
      window.visualViewport?.removeEventListener('scroll', placePanel)
    }
  }, [visible])

  useEffect(() => {
    if (!visible) return
    const closeOutside = (event: PointerEvent) => {
      const target = event.target as Node
      if (!panelRef.current?.contains(target) && !triggerRef.current?.contains(target)) setOpen(false)
    }
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setOpen(false)
      triggerRef.current?.focus()
    }
    const closeOnFocusOutside = (event: FocusEvent) => {
      const target = event.target as Node
      if (!panelRef.current?.contains(target) && !triggerRef.current?.contains(target)) setOpen(false)
    }
    document.addEventListener('pointerdown', closeOutside)
    document.addEventListener('keydown', closeOnEscape)
    document.addEventListener('focusin', closeOnFocusOutside)
    return () => {
      document.removeEventListener('pointerdown', closeOutside)
      document.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('focusin', closeOnFocusOutside)
    }
  }, [visible])

  const selectRange = (start: string, end: string) => {
    const from = start < end ? start : end
    const to = start < end ? end : start
    onChange(ordered.filter(meeting => dayKey(meeting.date) >= from && dayKey(meeting.date) <= to).map(meeting => meeting.clip_id))
  }

  const selectMeeting = (meeting: Meeting, shiftKey: boolean) => {
    const date = dayKey(meeting.date)
    const anchor = anchorRef.current ?? (shiftKey && selectedMeetings.length ? fromDate : null)
    if (anchor && (awaitingEndRef.current || shiftKey)) {
      selectRange(anchor, date)
      awaitingEndRef.current = false
    } else {
      anchorRef.current = date
      awaitingEndRef.current = true
      onChange([meeting.clip_id])
    }
  }

  const resetAnchor = () => {
    anchorRef.current = null
    awaitingEndRef.current = false
  }

  const selectPreset = (preset: 'last' | 'month' | 'year' | 'all') => {
    resetAnchor()
    if (preset === 'all') onChange([])
    else if (preset === 'last') onChange([ordered[ordered.length - 1].clip_id])
    else if (preset === 'year') selectRange(`${latestDate.slice(0, 4)}-01-01`, latestDate)
    else {
      const start = asDate(latestDate)
      start.setUTCDate(start.getUTCDate() - 29)
      selectRange(start.toISOString().slice(0, 10), latestDate)
    }
  }

  const changeBoundary = (boundary: 'from' | 'to', value: string) => {
    if (!value) return
    const target = asDate(value).getTime()
    if (!Number.isFinite(target)) return
    let nearest = firstDate
    let distance = Infinity
    for (const meeting of ordered) {
      const difference = Math.abs(asDate(meeting.date).getTime() - target)
      if (difference < distance) {
        nearest = dayKey(meeting.date)
        distance = difference
      }
    }
    resetAnchor()
    if (boundary === 'from') selectRange(nearest, nearest > toDate ? nearest : toDate)
    else selectRange(nearest < fromDate ? nearest : fromDate, nearest)
  }

  const navigateList = (event: KeyboardEvent<HTMLButtonElement>) => {
    const options = Array.from(panelRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])
    const index = options.indexOf(event.currentTarget)
    let next = index
    if (event.key === 'ArrowDown') next = Math.min(index + 1, options.length - 1)
    else if (event.key === 'ArrowUp') next = Math.max(index - 1, 0)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = options.length - 1
    else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      const meeting = ordered.find(item => item.clip_id === Number(event.currentTarget.dataset.meetingId))
      if (meeting) selectMeeting(meeting, event.shiftKey)
      return
    } else return
    event.preventDefault()
    options[next]?.focus()
  }

  const focusableId = ordered.some(meeting => meeting.clip_id === focusedId)
    ? focusedId
    : selectedMeetings[selectedMeetings.length - 1]?.clip_id ?? ordered[ordered.length - 1]?.clip_id

  return (
    <div className="meeting-range-filter">
      <button
        ref={triggerRef}
        type="button"
        className="meeting-range-trigger"
        aria-label={`Filter meetings: ${triggerLabel}`}
        aria-haspopup="dialog"
        aria-expanded={visible}
        aria-controls={dialogId}
        disabled={unavailable}
        onClick={() => { resetAnchor(); setOpen(!visible) }}
        onKeyDown={event => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault()
            resetAnchor()
            setOpen(true)
          }
        }}
      >{triggerLabel}</button>
      {!ordered.length && <span className="meeting-range-empty">No meetings available.</span>}
      {visible && createPortal(
        <div
          ref={panelRef}
          id={dialogId}
          className="meeting-range-panel"
          role="dialog"
          aria-label="Filter meetings"
          style={position}
        >
          <div className="meeting-range-presets">
            <button type="button" onClick={() => selectPreset('last')}>Last meeting</button>
            <button type="button" onClick={() => selectPreset('month')}>Last 30 days</button>
            <button type="button" onClick={() => selectPreset('year')}>This year</button>
            <button type="button" onClick={() => selectPreset('all')}>All</button>
          </div>
          <div className="meeting-range-dates">
            <label>From<input type="date" value={fromDate} onChange={event => changeBoundary('from', event.target.value)} /></label>
            <label>To<input type="date" value={toDate} onChange={event => changeBoundary('to', event.target.value)} /></label>
          </div>
          <div className="meeting-range-list" role="listbox" aria-label="Meetings" aria-multiselectable="true">
            {groups.map(group => (
              <div key={group.month} role="group" aria-label={monthDate.format(asDate(group.meetings[0].date))}>
                <div className="meeting-range-month" aria-hidden="true">{monthDate.format(asDate(group.meetings[0].date))}</div>
                {group.meetings.map(meeting => (
                  <button
                    key={meeting.clip_id}
                    type="button"
                    role="option"
                    aria-selected={selectedIds.length === 0 || selected.has(meeting.clip_id)}
                    tabIndex={meeting.clip_id === focusableId ? 0 : -1}
                    data-meeting-id={meeting.clip_id}
                    className="meeting-range-option"
                    onClick={event => selectMeeting(meeting, event.shiftKey)}
                    onKeyDown={navigateList}
                    onFocus={() => setFocusedId(meeting.clip_id)}
                  >
                    <span>{shortDate.format(asDate(meeting.date))}</span>
                    <span className="meeting-range-count"> · {meeting.n_items} {meeting.n_items === 1 ? 'item' : 'items'}</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="meeting-range-actions">
            <button type="button" onClick={() => selectPreset('all')}>Clear</button>
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
