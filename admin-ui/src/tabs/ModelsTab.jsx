import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

function ModelRows({ models }) {
  return models.map((x) => (
    <tr key={x.id}>
      <td>{x.id}</td>
      <td>{x.name}</td>
      <td><span className={`badge ${x.channel}`}>{x.channel}</span></td>
      <td>{x.context_window ?? '-'}</td>
      <td>{x.max_tokens ?? '-'}</td>
      <td>{x.supports_images ? '是' : '否'}</td>
    </tr>
  ));
}

export default function ModelsTab() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');

  const load = () =>
    api('/api/admin/models').then(setData).catch((e) => setErr(e.message));

  useEffect(() => { load(); }, []);

  const refresh = () =>
    api('/api/admin/models/refresh', { method: 'POST' }).then(load).catch((e) => setErr(e.message));

  if (err && !data) return <div className="muted">{err}</div>;
  if (!data) return <div className="muted">加载中…</div>;

  const rows = (c) => <ModelRows models={c} />;
  return (
    <>
      <div className="muted">{err}</div>
      <table>
        <thead>
          <tr><th>ID</th><th>名称</th><th>通道</th><th>上下文</th><th>最大输出</th><th>图片</th></tr>
        </thead>
        <tbody>
          <tr><th colSpan={6} className="muted">agent</th></tr>
          {rows(data.agent)}
          <tr><th colSpan={6} className="muted">benefit</th></tr>
          {rows(data.benefit)}
        </tbody>
      </table>
      <button className="primary" onClick={refresh}>强制刷新</button>
    </>
  );
}
