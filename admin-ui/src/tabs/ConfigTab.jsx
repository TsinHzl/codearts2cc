import React, { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function ConfigTab() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [cfgMsg, setCfgMsg] = useState('');
  const [snapMsg, setSnapMsg] = useState('');
  const [cfg, setCfg] = useState({ CODEARTS_ANTHROPIC_MODEL_MAP: '', CODEARTS_CACHE_SIM: '1' });
  const [snap, setSnap] = useState('');

  const load = () =>
    api('/api/admin/config').then((d) => {
      setData(d);
      setCfg({
        CODEARTS_ANTHROPIC_MODEL_MAP: d.editable?.CODEARTS_ANTHROPIC_MODEL_MAP || '',
        CODEARTS_CACHE_SIM: d.editable?.CODEARTS_CACHE_SIM !== '0' ? '1' : '0',
      });
      setSnap(''); // 掩码不载入输入框，仅作 placeholder 提示；提交掩码后端也会跳过
    }).catch((e) => setErr(e.message));

  useEffect(() => { load(); }, []);

  if (err && !data) return <div className="muted">{err}</div>;
  if (!data) return <div className="muted">加载中…</div>;

  const saveCfg = () =>
    api('/api/admin/config', {
      method: 'PUT',
      body: JSON.stringify({ editable: cfg }),
    })
      .then(() => setCfgMsg('已保存（立即生效）'))
      .catch((e) => setCfgMsg(e.message));

  const saveSnap = () =>
    api('/api/admin/config/secrets', {
      method: 'PUT',
      body: JSON.stringify({ secrets: { CODEARTS_SNAP_SESSION: snap } }),
    })
      .then((r) => setSnapMsg(r.updated && r.updated.length ? '已保存（立即生效）' : '未变更（掩码/纯 * 串不覆盖）'))
      .catch((e) => setSnapMsg(e.message));

  return (
    <div className="card">
      <h2>配置</h2>
      <label>模型映射 CODEARTS_ANTHROPIC_MODEL_MAP</label>
      <input
        type="text"
        value={cfg.CODEARTS_ANTHROPIC_MODEL_MAP}
        placeholder="alias:model,..."
        onChange={(e) => setCfg({ ...cfg, CODEARTS_ANTHROPIC_MODEL_MAP: e.target.value })}
      />
      <label>缓存模拟开关 CODEARTS_CACHE_SIM</label>
      <select value={cfg.CODEARTS_CACHE_SIM} onChange={(e) => setCfg({ ...cfg, CODEARTS_CACHE_SIM: e.target.value })}>
        <option value="1">开启</option>
        <option value="0">关闭</option>
      </select>
      <button className="primary" onClick={saveCfg}>保存</button>
      <div className="muted">{cfgMsg}</div>
      <hr />
      <h3>密钥（只读脱敏）</h3>
      <table>
        <tbody>
          <tr><th>CODEARTS_AK</th><td>{data.secrets?.CODEARTS_AK || '(未配置)'}</td></tr>
          <tr><th>CODEARTS_SK</th><td>{data.secrets?.CODEARTS_SK || '(未配置)'}</td></tr>
        </tbody>
      </table>
      <hr />
      <label>CODEARTS_SNAP_SESSION</label>
      <input
        type="password"
        value={snap}
        placeholder={data.secrets?.CODEARTS_SNAP_SESSION ? '已配置（粘贴新值覆盖）；掩码或纯 * 串不覆盖' : '粘贴新值；掩码或纯 * 串不覆盖'}
        onChange={(e) => setSnap(e.target.value)}
      />
      <button className="primary" onClick={saveSnap}>保存 session</button>
      <div className="muted">{snapMsg}</div>
    </div>
  );
}
