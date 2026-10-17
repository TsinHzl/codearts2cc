import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

const fmt = (n) => (n ?? 0).toLocaleString();
const fmtMs = (ms) => (ms == null ? '-' : ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms');
const fmtTs = (ts) => String(ts || '').replace('T', ' ');

export default function UsageTab() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [page, setPage] = useState(1);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api(`/api/admin/usage?page=${page}&limit=50`).then(setData).catch((e) => setErr(e.message));
  }, [page, tick]);

  if (err && !data) return <div className="muted">{err}</div>;
  if (!data) return <div className="muted">加载中…</div>;

  const s = data.summary || {};
  const groups = Object.entries(data.by_model || {});
  const total = data.total || 0;
  const pages = Math.max(1, Math.ceil(total / (data.limit || 50)));

  return (
    <>
      <div className="muted">{err}</div>
      <div className="card">
        <h2>使用统计 <button className="hbtn" onClick={() => setTick(tick + 1)}>刷新</button></h2>
        <div className="stats">
          <div className="stat"><b>{fmt(s.requests)}</b><span>总请求数</span></div>
          <div className="stat"><b>{fmt(s.input_tokens)}</b><span>输入 Tokens</span></div>
          <div className="stat"><b>{fmt(s.output_tokens)}</b><span>输出 Tokens</span></div>
          <div className="stat"><b>{fmt(s.cache_read_tokens)}</b><span>缓存读取 Tokens</span></div>
        </div>
      </div>
      {groups.length > 0 && (
        <div className="card">
          <h2>按模型分组</h2>
          <table>
            <thead><tr><th>模型</th><th>请求数</th><th>输入</th><th>输出</th><th>缓存读取</th></tr></thead>
            <tbody>
              {groups.map(([mo, g]) => (
                <tr key={mo}>
                  <td>{mo}</td>
                  <td>{fmt(g.requests)}</td>
                  <td>{fmt(g.input_tokens)}</td>
                  <td>{fmt(g.output_tokens)}</td>
                  <td>{fmt(g.cache_read_tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="card">
        <h2>请求明细 <span className="muted">{total} 条，第 {data.page}/{pages} 页</span></h2>
        <table>
          <thead>
            <tr><th>时间</th><th>IP</th><th>端点</th><th>模型</th><th>标签</th><th>输入</th><th>输出</th><th>缓存读取</th><th>输入总计</th><th>首字</th><th>总耗时</th></tr>
          </thead>
          <tbody>
            {(data.entries || []).map((e, i) => {
              const totalIn = (e.input_tokens || 0) + (e.cache_read_tokens || 0);
              return (
                <tr key={i}>
                  <td>{fmtTs(e.ts)}</td>
                  <td>{e.ip || '-'}</td>
                  <td><span className={`badge ${e.endpoint}`}>{e.endpoint}</span></td>
                  <td>{e.model}</td>
                  <td>
                    <span className={`badge ${e.stream ? 'stream' : 'plain'}`}>{e.stream ? '流式' : '非流式'}</span>
                    {e.effort && <span> <span className="badge plain">{e.effort}</span></span>}
                  </td>
                  <td>{fmt(e.input_tokens)}</td>
                  <td>{fmt(e.output_tokens)}</td>
                  <td>{fmt(e.cache_read_tokens)}</td>
                  <td>{fmt(totalIn)}</td>
                  <td>{fmtMs(e.first_token_ms)}</td>
                  <td>{fmtMs(e.duration_ms)}</td>
                </tr>
              );
            })}
            {(!data.entries || data.entries.length === 0) && (
              <tr><td colSpan={11} className="muted">暂无记录</td></tr>
            )}
          </tbody>
        </table>
        <div className="pager">
          <button disabled={data.page <= 1} onClick={() => setPage(data.page - 1)}>上一页</button>
          <button disabled={data.page >= pages} onClick={() => setPage(data.page + 1)}>下一页</button>
        </div>
      </div>
    </>
  );
}
