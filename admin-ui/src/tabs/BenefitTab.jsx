import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

function CreditCard() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');

  const load = () => {
    setData(null); setErr('');
    api('/api/admin/credit').then(setData).catch((e) => setErr(e.message));
  };
  useEffect(() => { load(); }, []);

  let body;
  if (err) {
    body = <div className="muted">{err}</div>;
  } else if (!data) {
    body = <div className="muted">加载中…</div>;
  } else {
    const a = data.amount, u = data.used;
    const ok = typeof a === 'number' && a > 0 && typeof u === 'number';
    const pct = ok ? Math.min(100, Math.max(0, (u / a) * 100)) : null;
    body = (
      <>
        <table>
          <tbody>
            <tr><th>总量</th><td>{String(data.amount)}</td></tr>
            <tr><th>已用</th><td>{String(data.used)}</td></tr>
            <tr><th>剩余</th><td>{String(data.remain)}</td></tr>
          </tbody>
        </table>
        <div className="credit-bar"><i style={{ width: pct == null ? '0%' : pct.toFixed(1) + '%' }} /></div>
        <div className="muted" style={{ fontSize: 12 }}>{pct == null ? '--' : `已用 ${pct.toFixed(1)}%`}</div>
      </>
    );
  }
  return (
    <div className="card">
      <h2>积分用量 <button className="hbtn" onClick={load}>刷新</button></h2>
      {body}
    </div>
  );
}

export default function BenefitTab() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [claimMsg, setClaimMsg] = useState('');
  const [claiming, setClaiming] = useState(false);

  const load = () =>
    api('/api/admin/benefit').then(setData).catch((e) => setErr(e.message));

  useEffect(() => { load(); }, []);

  const claim = () => {
    setClaiming(true);
    api('/api/admin/benefit/claim', { method: 'POST' })
      .then(() => { setClaimMsg('签到成功'); setClaiming(false); load(); })
      .catch((e) => { setClaiming(false); setClaimMsg(e.message); });
  };

  return (
    <>
      <div className="card">
        <h2>免费额度 <button className="hbtn" onClick={load}>刷新</button></h2>
        <table>
          <tbody>
            <tr><th>总配额</th><td>{String(data?.total_quota ?? '')}</td></tr>
            <tr><th>剩余</th><td>{String(data?.total_balance ?? '')}</td></tr>
            <tr><th>已用</th><td>{String(data?.used_amount ?? '')}</td></tr>
          </tbody>
        </table>
        <button className="primary" onClick={claim} disabled={claiming}>立即签到</button>
        <div className="muted">{claimMsg}</div>
      </div>
      <CreditCard />
    </>
  );
}
