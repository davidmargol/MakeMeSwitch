import React, { useEffect, useState } from 'react'
import { AlertTriangle, Check, X } from 'lucide-react'
import type { Op } from '../api'

export function Card({ className = '', children }: { className?: string; children: React.ReactNode }) {
  return <div className={`card ${className}`}>{children}</div>
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

const BADGE_STYLES: Record<string, string> = {
  up: 'border-ok/40 bg-ok/10 text-ok',
  active: 'border-ok/40 bg-ok/10 text-ok',
  connected: 'border-ok/40 bg-ok/10 text-ok',
  success: 'border-ok/40 bg-ok/10 text-ok',
  down: 'border-edge bg-panel2 text-muted',
  'not connected': 'border-edge bg-panel2 text-muted',
  'not present': 'border-edge bg-panel2 text-muted',
  absent: 'border-edge bg-panel2 text-muted',
  warning: 'border-warn/40 bg-warn/10 text-warn',
  info: 'border-accent2/40 bg-accent2/10 text-accent2',
  error: 'border-bad/40 bg-bad/10 text-bad',
  critical: 'border-bad/40 bg-bad/10 text-bad',
  failure: 'border-bad/40 bg-bad/10 text-bad',
  'administratively down': 'border-bad/40 bg-bad/10 text-bad',
  unassigned: 'border-warn/40 bg-warn/10 text-warn',
  access: 'border-accent2/40 bg-accent2/10 text-accent2',
  trunk: 'border-accent/40 bg-accent/10 text-accent',
  admin: 'border-accent/40 bg-accent/10 text-accent',
  operator: 'border-accent2/40 bg-accent2/10 text-accent2',
  readonly: 'border-edge bg-panel2 text-muted',
}

export function Badge({ children, tone }: { children: React.ReactNode; tone?: string }) {
  const key = String(tone ?? children).toLowerCase()
  const style = BADGE_STYLES[key] ?? 'border-edge bg-panel2 text-muted'
  return <span className={`badge ${style}`}>{children}</span>
}

export function StatCard({
  label,
  value,
  sub,
  icon,
  tone = 'text-ink',
}: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  icon?: React.ReactNode
  tone?: string
}) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className={`mt-1.5 truncate text-2xl font-semibold tracking-tight ${tone}`}>{value}</div>
          {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
        </div>
        {icon && <div className="rounded-lg border border-edge bg-panel2 p-2 text-accent">{icon}</div>}
      </div>
    </Card>
  )
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-12 text-center">
      <div className="text-sm font-medium text-muted">{title}</div>
      {hint && <div className="max-w-md text-xs text-muted/70">{hint}</div>}
    </div>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: React.ReactNode
  footer?: React.ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    if (open) window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 backdrop-blur-sm sm:p-8">
      <div className={`card w-full ${wide ? 'max-w-4xl' : 'max-w-xl'} my-auto`}>
        <div className="flex items-center justify-between border-b border-edge px-5 py-3.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button className="text-muted transition hover:text-ink" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-edge px-5 py-3">{footer}</div>}
      </div>
    </div>
  )
}

export function ConfirmDialog({
  open,
  title = 'Confirm change',
  message,
  ops,
  warnings,
  confirmLabel = 'Apply',
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title?: string
  message: string
  ops?: Op[]
  warnings?: string[]
  confirmLabel?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      footer={
        <>
          <button className="btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button className="btn-primary" onClick={onConfirm} disabled={busy}>
            <Check size={15} /> {busy ? 'Applying…' : confirmLabel}
          </button>
        </>
      }
    >
      <div className="flex items-start gap-3">
        <div className="rounded-lg border border-warn/40 bg-warn/10 p-2 text-warn">
          <AlertTriangle size={18} />
        </div>
        <div className="text-sm text-ink/90">{message}</div>
      </div>
      {warnings && warnings.length > 0 && (
        <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-warn">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {ops && ops.length > 0 && (
        <div className="mt-4 max-h-56 overflow-auto rounded-lg border border-edge bg-panel2 p-3">
          {ops.map((op, idx) => (
            <div key={idx} className="mono flex gap-2 py-0.5 text-[12px]">
              <span className="text-accent">{op.action}</span>
              <span className="text-ink">{op.target}</span>
              <span className="truncate text-muted">{op.detail}</span>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

export function Toggle({ checked, onChange, disabled }: { checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full border transition ${
        checked ? 'border-accent/60 bg-accent/30' : 'border-edge bg-panel2'
      } disabled:opacity-40`}
      aria-pressed={checked}
    >
      <span
        className={`absolute top-0.5 h-4.5 w-4.5 rounded-full bg-ink transition-all ${checked ? 'left-[22px] bg-accent' : 'left-0.5'}`}
        style={{ height: 18, width: 18 }}
      />
    </button>
  )
}

export function useAsyncAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async <T,>(fn: () => Promise<T>, rethrow = false): Promise<T | undefined> => {
    setBusy(true)
    setError(null)
    try {
      return await fn()
    } catch (exc: any) {
      if (rethrow) throw exc
      setError(exc?.message ?? String(exc))
      return undefined
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, setError, run }
}

export function Toast({ message, tone = 'ok', onDone }: { message: string | null; tone?: 'ok' | 'bad'; onDone: () => void }) {
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(onDone, 3200)
    return () => clearTimeout(timer)
  }, [message, onDone])
  if (!message) return null
  return (
    <div
      className={`fixed bottom-5 right-5 z-[60] flex items-center gap-2 rounded-lg border px-4 py-2.5 text-sm shadow-panel ${
        tone === 'ok' ? 'border-ok/40 bg-ok/15 text-ok' : 'border-bad/40 bg-bad/15 text-bad'
      }`}
    >
      {tone === 'ok' ? <Check size={16} /> : <AlertTriangle size={16} />}
      {message}
    </div>
  )
}
