// cost-sentinel — Desktop half: right pane + status-bar chip.
// Data comes from this package's own backend (dashboard/plugin_api.py) via
// ctx.rest; budget toggles POST /budget. Live updates arrive as
// plugin.cost-sentinel.cost.updated events pushed by the agent half.
import { host, haptic } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let rest = null // bound in register(); pane/chip use the bound one

const usd = (value) =>
  value === null || value === undefined
    ? '—'
    : Number(value).toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 4 })

function SpendRing({ ratio }) {
  const pct = Math.max(0, Math.min(1, ratio)) * 100
  return jsxs('div', {
    className: 'flex items-center gap-3',
    children: [
      jsx('div', {
        className: 'h-9 w-9 shrink-0 rounded-full',
        style: {
          background: `conic-gradient(var(--ui-accent) ${pct}%, var(--ui-stroke-secondary) ${pct}%)`,
          mask: 'radial-gradient(circle 14px, transparent 13px, black 14px)',
          WebkitMask: 'radial-gradient(circle 14px, transparent 13px, black 14px)'
        }
      }),
      jsx('div', {
        children: [
          jsx('div', { className: 'font-medium', children: `${Math.round(pct)}%` }),
          jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'of daily limit' })
        ]
      })
    ]
  })
}

function Pane() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    const load = async () => {
      try {
        const next = await rest('/summary?days=7')
        if (live) { setData(next); setError('') }
      } catch (e) {
        if (live) setError(String(e && e.message ? e.message : e))
      }
    }
    load()
    const timer = setInterval(load, 10000)
    // The agent half pushes 'cost.updated' after each priced request; a missing
    // push just means the 10s poll keeps the pane fresh.
    let offPush = null
    if (typeof host?.onEvent === 'function') {
      offPush = host.onEvent('plugin.cost-sentinel.cost.updated', load)
    }
    return () => { live = false; clearInterval(timer); if (typeof offPush === 'function') offPush() }
  }, [])

  if (error && !data) {
    return jsx('div', { className: 'p-3 text-sm text-(--ui-text-tertiary)',
      children: `backend unavailable (${error}) — enable the agent half too` })
  }
  if (!data) return jsx('div', { className: 'p-3 text-sm text-(--ui-text-tertiary)', children: 'loading…' })

  const limit = data.daily_limit_usd
  const gateSpend = data.scope === 'fleet' ? data.fleet_today_spend_usd : data.today_spend_usd
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm overflow-y-auto',
    children: [
      jsx('div', { className: 'font-medium', children: 'Cost Sentinel' }),
      jsx(SpendRing, { ratio: limit > 0 ? gateSpend / limit : 0 }),
      jsxs('div', { children: [
        jsxs('div', { children: [usd(gateSpend), limit > 0 ? ` of ${usd(limit)} (${data.scope})` : ''] }),
        jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
          children: data.scope === 'fleet'
            ? `${data.today_known_rows} this profile · fleet ${usd(data.fleet_today_spend_usd)} · ${data.enforce}${data.over_budget ? ' · OVER BUDGET' : ''}`
            : `${data.today_known_rows} priced calls · ${data.enforce}${data.over_budget ? ' · OVER BUDGET' : ''}` })
      ]}),
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Top models — 7 days' }),
      ...(data.by_model || []).slice(0, 5).map((m) => jsx('div',
        { key: `${m.model}-${m.provider}`, className: 'flex justify-between gap-2',
          children: [
            jsx('span', { className: 'truncate', children: m.model || 'unknown' }),
            jsx('span', { className: 'tabular-nums text-(--ui-text-secondary)', children: usd(m.cost_usd) })
          ]})),
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Last 7 days (UTC)' }),
      ...(data.days || []).slice(-7).map((d) => jsx('div', { key: d.day, className: 'flex justify-between gap-2',
        children: [
          jsx('span', { className: 'text-(--ui-text-secondary)', children: d.day }),
          jsx('span', { className: 'tabular-nums', children: usd(d.cost_usd) })
        ]})),
      jsxs('div', { className: 'mt-auto flex gap-2', children: [
        jsx('button', {
          type: 'button',
          className: 'rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-[0.6875rem]',
          onClick: async () => {
            const nextEnforce = data.enforce === 'block' ? 'warn' : 'block'
            await rest('/budget', { method: 'POST', body: { enforce: nextEnforce } })
            setData(await rest('/summary?days=7'))
          },
          children: data.enforce === 'block' ? 'Hard gate: on' : 'Hard gate: off'
        }),
        jsx('button', {
          type: 'button',
          className: 'rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-[0.6875rem]',
          onClick: async () => {
            await rest('/budget', { method: 'POST', body: { daily_limit_usd: 0, enforce: 'off' } })
            setData(await rest('/summary?days=7'))
          },
          children: 'No limit'
        })
      ]})
    ]
  })
}

function Chip() {
  const [data, setData] = useState(null)
  useEffect(() => {
    let live = true
    const load = async () => {
      try {
        const next = await rest('/budget')
        if (!live) return
        setData(next && next.daily_limit_usd > 0 && next.enforce !== 'off' ? next : null)
      } catch { if (live) setData(null) }
    }
    load()
    const timer = setInterval(load, 30000)
    return () => { live = false; clearInterval(timer) }
  }, [])
  if (!data) return null
  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    onClick: () => { haptic('tap'); host.notify({ kind: 'info', message: 'cost-sentinel: watching the daily budget' }) },
    children: data.enforce === 'block' ? `$${data.daily_limit_usd}/day ⏳` : `$${data.daily_limit_usd}/day`
  })
}

export default {
  id: 'cost-sentinel',
  name: 'Cost Sentinel',
  register(pctx) {
    rest = (path, init) => pctx.rest(path, init)
    pctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Cost Sentinel',
      data: { placement: 'right', width: '260px' },
      render: () => jsx(Pane, {})
    })
    pctx.register({
      id: 'cost-chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(Chip, {})
    })
  }
}
