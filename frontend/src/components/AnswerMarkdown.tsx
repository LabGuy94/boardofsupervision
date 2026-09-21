import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CitationChip, type InlineCitation, type SeekCitation } from './CitationChip'

// Transform text nodes only: code and existing links retain their literal content.
type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] }
function remarkCitations() {
  return (tree: MarkdownNode) => {
    function visit(node: MarkdownNode) {
      if (!node.children || node.type === 'link' || node.type === 'code' || node.type === 'inlineCode') return
      node.children = node.children.flatMap((child) => {
        if (child.type !== 'text' || !child.value) { visit(child); return [child] }
        const pieces: MarkdownNode[] = []
        let offset = 0
        for (const match of child.value.matchAll(/\[\[(\d+:\d+)(?:@(\d+(?:\.\d+)?))?\]\]/g)) {
          if (match.index! > offset) pieces.push({ type: 'text', value: child.value.slice(offset, match.index) })
          pieces.push({ type: 'link', url: `#citation-${match[1]}${match[2] ? `@${match[2]}` : ''}`, children: [{ type: 'text', value: match[1] }] })
          offset = match.index! + match[0].length
        }
        if (offset < child.value.length) pieces.push({ type: 'text', value: child.value.slice(offset) })
        return pieces
      })
    }
    visit(tree)
  }
}

type TextNode = { value?: unknown; children?: TextNode[] }
function nodeText(node?: TextNode): string {
  return typeof node?.value === 'string' ? node.value : (node?.children ?? []).map(nodeText).join('')
}
const numeric = /^[$\d,.%−-]+$/

export function AnswerMarkdown({ markdown, citations = [], onSeek }: { markdown: string; citations?: InlineCitation[]; onSeek: SeekCitation }) {
  const byKey = new Map(citations.map((citation) => [citation.key, citation]))
  return <div className="prose"><Markdown remarkPlugins={[remarkGfm, remarkCitations]} components={{
    h1: ({ children }) => <h3>{children}</h3>,
    h2: ({ children }) => <h3>{children}</h3>,
    h4: ({ children }) => <h3>{children}</h3>,
    h5: ({ children }) => <h3>{children}</h3>,
    h6: ({ children }) => <h3>{children}</h3>,
    table: ({ children }) => <div className="prose-table-scroll" role="region" aria-label="Answer data table" tabIndex={0}><table>{children}</table></div>,
    td: ({ node, children, style }) => <td style={style} className={numeric.test(nodeText(node).trim()) ? 'numeric' : undefined}>{children}</td>,
    th: ({ node, children, style }) => <th style={style} className={numeric.test(nodeText(node).trim()) ? 'numeric' : undefined}>{children}</th>,
    tr: ({ node, children }) => <tr className={/^Total\b/i.test(nodeText(node?.children.find((child) => child.type === 'element')).trim()) ? 'total-row' : undefined}>{children}</tr>,
    a: ({ href, children }) => {
      if (href?.startsWith('#citation-')) {
        const [key, time] = href.slice(10).split('@')
        const citation = byKey.get(key)
        return citation ? <CitationChip citation={citation} seconds={time === undefined ? undefined : Number(time)} onSeek={onSeek} /> : null
      }
      return href ? <a href={href} target="_blank" rel="noreferrer">{children} <span aria-hidden="true">↗</span></a> : <span>{children}</span>
    },
  }}>{markdown}</Markdown></div>
}
