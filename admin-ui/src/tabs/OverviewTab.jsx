import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function OverviewTab() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    api('/api/admin/overview').then(setData).catch((e) => setErr(e.message));
  }, []);

  if (err) return <main><div className="muted">{err}</div></main>;
  if (!data) return <main><div className="muted">加载中…</div></main>;
  return (
    <div className="card">
      <h2>概览</h2>
      {data.public_exposed && <div className="warn">⚠️ 监听地址非回环，管理面已暴露公网</div>}
      <table>
        <tbody>
          <tr><th>监听</th><td>{data.listen}</td></tr>
          <tr><th>上游</th><td>{data.upstream}</td></tr>
          <tr><th>模型数</th><td>{data.model_count}（agent {data.agent_count} / benefit {data.benefit_count}）</td></tr>
          <tr><th>缓存模拟</th><td>{data.cache_sim_enabled ? '开启' : '关闭'}</td></tr>
        </tbody>
      </table>
    </div>
  );
}
