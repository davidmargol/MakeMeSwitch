import { useRef, useState } from 'react'
import { useLiveRefresh } from '../useLiveRefresh'
import { AlertTriangle, Check, Download, History, RotateCcw, Save, Upload } from 'lucide-react'
import { api, ConfirmError, hasRole, type ConfigBundle, type Op, type Session } from '../api'
import { Badge, Card, ConfirmDialog, Empty, PageHeader, Toast, useAsyncAction } from '../components/ui'

type Tab = 'running' | 'startup' | 'diff'

function lineDiff(left: string, right: string): { type: 'same' | 'add' | 'del'; text: string }[] {
  const a = left.split('\n')
  const b = right.split('\n')
  const dp: number[][] = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0))
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: { type: 'same' | 'add' | 'del'; text: string }[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i++] })
    } else {
      out.push({ type: 'add', text: b[j++] })
    }
  }
  while (i < a.length) out.push({ type: 'del', text: a[i++] })
  while (j < b.length) out.push({ type: 'add', text: b[j++] })
  return out
}

export default function Configuration({ session }: { session: Session }) {
  const [bundle, setBundle] = useState<ConfigBundle | null>(null)
  const [tab, setTab] = useState<Tab>('running')
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmState, setConfirmState] = useState<{ message: string; ops: Op[]; retry: () => Promise<void> } | null>(null)
  const [rollbackOpen, setRollbackOpen] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const { busy, run } = useAsyncAction()
  const canEdit = hasRole(session, 'operator')

  const load = useLiveRefresh(async () => {
    try {
      const data = await api.get<ConfigBundle>('/api/v1/config')
      setBundle(data)
      setError(null)
    } catch (exc: any) {
      setError(exc.message)
    }
  })

  const save = () =>
    run(async () => {
      await api.post('/api/v1/config/save')
      setToast('Startup configuration saved')
      await load()
    })

  const importFile = async (file: File) => {
    const text = await file.text()
    let parsed: any
    try {
      parsed = JSON.parse(text)
    } catch {
      setError('The selected file is not valid JSON')
      return
    }
    const config = parsed?.config ?? parsed
    try {
      await api.post('/api/v1/config/import', { config, confirm: false })
      setToast('Configuration imported and applied')
      await load()
    } catch (exc: any) {
      if (exc instanceof ConfirmError) {
        setConfirmState({
          message: exc.message,
          ops: exc.preview.ops,
          retry: async () => {
            await api.post('/api/v1/config/import', { config, confirm: true })
            setToast('Configuration imported and applied')
            await load()
          },
        })
      } else {
        setError(exc?.message ?? String(exc))
      }
    }
  }

  const rollback = async (historyId?: number) => {
    try {
      await api.post('/api/v1/config/rollback', { history_id: historyId ?? null })
      setToast('Rollback applied')
      setRollbackOpen(false)
      await load()
    } catch (exc: any) {
      if (exc instanceof ConfirmError) {
        setRollbackOpen(false)
        setConfirmState({
          message: exc.message,
          ops: exc.preview.ops,
          retry: async () => {
            await api.post('/api/v1/config/rollback', { history_id: historyId ?? null, confirm: true })
            setToast('Rollback applied')
            await load()
          },
        })
      } else {
        setError(exc?.message ?? String(exc))
      }
    }
  }

  if (error && !bundle) return <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>
  if (!bundle) return <Empty title="Loading configuration…" />

  const diff = bundle.startup ? lineDiff(bundle.startup_text, bundle.running_text) : []

  return (
    <div className="space-y-5">
      <PageHeader
        title="Configuration"
        subtitle="Running and startup configuration share one engine with the CLI"
        actions={
          <>
            {bundle.unsaved_changes ? <Badge tone="warning">unsaved changes</Badge> : <Badge tone="success">saved</Badge>}
            <a className="btn" href="/api/v1/config/export" download>
              <Download size={15} /> Export
            </a>
            {canEdit && (
              <>
                <input
                  ref={fileRef}
                  type="file"
                  accept="application/json,.json"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) importFile(file)
                    e.target.value = ''
                  }}
                />
                <button className="btn" onClick={() => fileRef.current?.click()}>
                  <Upload size={15} /> Import
                </button>
                <button className="btn" onClick={() => setRollbackOpen(true)}>
                  <History size={15} /> Rollback
                </button>
                <button className="btn-primary" onClick={save} disabled={busy}>
                  <Save size={15} /> Write memory
                </button>
              </>
            )}
          </>
        }
      />

      {error && <Card className="border-bad/40 p-4 text-sm text-bad">{error}</Card>}

      {bundle.drift.length > 0 && (
        <Card className="border-warn/40 p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-warn">
            <AlertTriangle size={15} /> Drift detected between running configuration and OVS
          </div>
          <ul className="space-y-1 text-xs text-muted">
            {bundle.drift.map((item, index) => (
              <li key={index}>
                <span className="text-warn">{item.severity}</span> {item.port ? `${item.port}: ` : ''}
                {item.message}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card className="overflow-hidden">
        <div className="flex items-center gap-1 border-b border-edge px-3 py-2">
          {(['running', 'startup', 'diff'] as Tab[]).map((item) => (
            <button
              key={item}
              onClick={() => setTab(item)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium capitalize transition ${
                tab === item ? 'bg-accent/10 text-accent' : 'text-muted hover:text-ink'
              }`}
            >
              {item === 'diff' ? 'differences' : item}
            </button>
          ))}
          <div className="ml-auto text-[11px] text-muted">
            {tab === 'running' ? `${bundle.running_text.split('\n').length} lines` : tab === 'startup' ? `${bundle.startup_text.split('\n').length} lines` : `${diff.length} lines`}
          </div>
        </div>
        <div className="max-h-[60vh] overflow-auto bg-[rgb(var(--c-bg))]/60 p-4">
          {tab === 'diff' ? (
            diff.length === 0 ? (
              <Empty title="Running and startup configuration are identical" />
            ) : (
              <pre className="mono text-[12px] leading-5">
                {diff.map((line, index) => (
                  <div
                    key={index}
                    className={
                      line.type === 'add'
                        ? 'bg-ok/10 text-ok'
                        : line.type === 'del'
                          ? 'bg-bad/10 text-bad'
                          : 'text-muted'
                    }
                  >
                    {line.type === 'add' ? '+ ' : line.type === 'del' ? '- ' : '  '}
                    {line.text}
                  </div>
                ))}
              </pre>
            )
          ) : (
            <pre className="mono whitespace-pre-wrap text-[12px] leading-5 text-ink/90">
              {tab === 'running' ? bundle.running_text : bundle.startup_text || '! no startup configuration'}
            </pre>
          )}
        </div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <RotateCcw size={15} /> Configuration lifecycle
          </div>
          <ul className="space-y-1.5 text-xs text-muted">
            <li>• Changes are validated, applied to OVS and stored atomically.</li>
            <li>• <span className="text-ink">Write memory</span> copies running → startup.</li>
            <li>• A failed apply is rolled back to the pre-change OVS snapshot.</li>
            <li>• <span className="text-ink">Reload</span> from the CLI re-applies the startup configuration.</li>
          </ul>
        </Card>
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <History size={15} /> Recent changes
          </div>
          <div className="space-y-1.5">
            {bundle.history.slice(0, 5).map((entry) => (
              <div key={entry.id} className="flex items-center justify-between text-xs">
                <span className="text-muted">#{entry.id}</span>
                <span className="flex-1 px-2 truncate">{entry.action}</span>
                <span className="text-muted">{entry.user}</span>
                <span className="ml-2 text-muted">{new Date(entry.ts * 1000).toLocaleTimeString()}</span>
              </div>
            ))}
            {bundle.history.length === 0 && <div className="text-xs text-muted">No history yet</div>}
          </div>
        </Card>
      </div>

      {rollbackOpen && (
        <Card className="fixed inset-0 z-50 m-auto h-fit max-h-[70vh] w-full max-w-lg overflow-auto p-0">
          <div className="flex items-center justify-between border-b border-edge px-4 py-3">
            <div className="text-sm font-semibold">Roll back configuration</div>
            <button className="text-muted hover:text-ink" onClick={() => setRollbackOpen(false)}>
              ✕
            </button>
          </div>
          <div className="divide-y divide-edge">
            {bundle.history.map((entry) => (
              <button
                key={entry.id}
                className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm hover:bg-panel2"
                onClick={() => rollback(entry.id)}
              >
                <span>#{entry.id} · {entry.action}</span>
                <span className="text-xs text-muted">
                  {new Date(entry.ts * 1000).toLocaleString()} · {entry.user}
                </span>
              </button>
            ))}
            {bundle.history.length === 0 && <Empty title="No history entries" />}
          </div>
          <div className="border-t border-edge px-4 py-3 text-right">
            <button className="btn" onClick={() => rollback(undefined)}>
              <Check size={14} /> Roll back to previous
            </button>
          </div>
        </Card>
      )}

      <ConfirmDialog
        open={!!confirmState}
        title="Confirm configuration change"
        message={confirmState?.message ?? ''}
        ops={confirmState?.ops}
        onCancel={() => setConfirmState(null)}
        onConfirm={async () => {
          if (!confirmState) return
          const retry = confirmState.retry
          setConfirmState(null)
          try {
            await retry()
          } catch (exc: any) {
            setError(exc?.message ?? String(exc))
          }
        }}
      />
      <Toast message={toast} onDone={() => setToast(null)} />
    </div>
  )
}
