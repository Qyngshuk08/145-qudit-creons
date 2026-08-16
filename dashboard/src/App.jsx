import { useState, useEffect } from 'react'
import './App.css'

const API_BASE = 'http://127.0.0.1:8000'

function riskColor(score) {
  if (score >= 70) return '#dc2626'
  if (score >= 40) return '#f59e0b'
  return '#65a30d'
}

function AccountList({ accounts, selectedId, onSelect, loading, error }) {
  if (loading) return <div className="panel-message">Loading flagged accounts...</div>
  if (error) return <div className="panel-message error">Error: {error}</div>
  if (!accounts.length) return <div className="panel-message">No flagged accounts above threshold.</div>

  return (
    <div className="account-list">
      {accounts.map((a) => (
        <div
          key={a.account_id}
          className={`account-row ${selectedId === a.account_id ? 'selected' : ''}`}
          onClick={() => onSelect(a.account_id)}
        >
          <div className="account-row-top">
            <span className="account-id">{a.account_id}</span>
            <span className="risk-badge" style={{ background: riskColor(a.risk_score) }}>
              {a.risk_score}
            </span>
          </div>
          <div className="account-patterns">
            {a.triggered_patterns.split(';').map((p) => (
              <span key={p} className="pattern-chip">{p.replace(/_/g, ' ')}</span>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ExplainPanel({ accountId }) {
  const [state, setState] = useState({ status: 'idle', text: null, source: null })

  // Reset when the selected account changes -- don't show a stale explanation
  useEffect(() => {
    setState({ status: 'idle', text: null, source: null })
  }, [accountId])

  const fetchExplanation = () => {
    setState({ status: 'loading', text: null, source: null })
    fetch(`${API_BASE}/accounts/${accountId}/explain`)
      .then(async (r) => {
        const data = await r.json()
        if (!r.ok) throw new Error(data.detail || `API returned ${r.status}`)
        return data
      })
      .then((data) => setState({ status: 'done', text: data.explanation, source: data.source }))
      .catch((e) => setState({ status: 'error', text: e.message, source: null }))
  }

  return (
    <div className="detail-section explain-section">
      <h3>
        Investigator Explanation
        {state.source && (
          <span className={`source-tag ${state.source.startsWith('live') ? 'live' : ''}`}>
            {state.source}
          </span>
        )}
      </h3>
      {state.status === 'idle' && (
        <button className="explain-button" onClick={fetchExplanation}>
          Generate explanation (Nemotron)
        </button>
      )}
      {state.status === 'loading' && <p className="evidence-text loading">Generating narrative...</p>}
      {state.status === 'error' && <p className="evidence-text error-text">{state.text}</p>}
      {state.status === 'done' && <p className="evidence-text explanation-text">{state.text}</p>}
    </div>
  )
}

function AccountDetail({ accountId }) {
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!accountId) return
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/accounts/${accountId}`)
      .then((r) => {
        if (!r.ok) throw new Error(`API returned ${r.status}`)
        return r.json()
      })
      .then((data) => setDetail(data))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [accountId])

  if (!accountId) return <div className="panel-message">Select an account from the list to investigate.</div>
  if (loading) return <div className="panel-message">Loading live transaction data for {accountId}...</div>
  if (error) return <div className="panel-message error">Error: {error}</div>
  if (!detail) return null

  return (
    <div className="detail-panel">
      <div className="detail-header">
        <h2>{detail.account_id}</h2>
        <span className="risk-badge large" style={{ background: riskColor(detail.risk_score) }}>
          Risk: {detail.risk_score}
        </span>
      </div>

      <div className="detail-section">
        <h3>Triggered Patterns <span className="source-tag">precomputed</span></h3>
        <div className="account-patterns">
          {detail.triggered_patterns
            ? detail.triggered_patterns.split(';').map((p) => (
                <span key={p} className="pattern-chip">{p.replace(/_/g, ' ')}</span>
              ))
            : <span className="pattern-chip none">not flagged</span>}
        </div>
      </div>

      <div className="detail-section">
        <h3>Evidence <span className="source-tag">precomputed</span></h3>
        <p className="evidence-text">{detail.evidence}</p>
      </div>

      <ExplainPanel accountId={detail.account_id} />

      <div className="detail-section">
        <h3>Recent Incoming <span className="source-tag live">live &middot; KuzuDB</span></h3>
        <TxnTable txns={detail.live_incoming_transactions} />
      </div>

      <div className="detail-section">
        <h3>Recent Outgoing <span className="source-tag live">live &middot; KuzuDB</span></h3>
        <TxnTable txns={detail.live_outgoing_transactions} />
      </div>
    </div>
  )
}

function TxnTable({ txns }) {
  if (!txns || !txns.length) return <p className="empty-txns">None found.</p>
  return (
    <table className="txn-table">
      <thead>
        <tr><th>Counterparty</th><th>Type</th><th>Amount</th><th>Category</th><th>Timestamp</th></tr>
      </thead>
      <tbody>
        {txns.map((t, i) => (
          <tr key={i}>
            <td>{t.counterparty}</td>
            <td>{t.counterparty_type}</td>
            <td>{t.amount?.toFixed(2)}</td>
            <td>{t.category}</td>
            <td>{t.timestamp}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function App() {
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [minScore, setMinScore] = useState(30)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/accounts/flagged?min_score=${minScore}&limit=50`)
      .then((r) => {
        if (!r.ok) throw new Error(`API returned ${r.status}. Is the API running on ${API_BASE}?`)
        return r.json()
      })
      .then((data) => setAccounts(data))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [minScore])

  return (
    <div className="app">
      <header className="app-header">
        <h1>Mule Network Investigator</h1>
        <span className="subtitle">Use Case #47 &middot; Team Qudit Creons</span>
      </header>
      <div className="app-body">
        <aside className="sidebar">
          <div className="sidebar-controls">
            <label>Min risk score: {minScore}</label>
            <input
              type="range" min="0" max="100" value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
            />
            <span className="count-label">{accounts.length} flagged accounts</span>
          </div>
          <AccountList
            accounts={accounts}
            selectedId={selectedId}
            onSelect={setSelectedId}
            loading={loading}
            error={error}
          />
        </aside>
        <main className="main-panel">
          <AccountDetail accountId={selectedId} />
        </main>
      </div>
    </div>
  )
}