import React, { useEffect, useState } from 'react';
import { api } from './api.js';
import OverviewTab from './tabs/OverviewTab.jsx';
import ModelsTab from './tabs/ModelsTab.jsx';
import BenefitTab from './tabs/BenefitTab.jsx';
import UsageTab from './tabs/UsageTab.jsx';
import ConfigTab from './tabs/ConfigTab.jsx';

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'models', label: '模型' },
  { key: 'benefit', label: '额度' },
  { key: 'usage', label: '使用日志' },
  { key: 'config', label: '配置' },
];

export default function App() {
  const [authed, setAuthed] = useState(false);
  const [tab, setTab] = useState('overview');
  const [loginErr, setLoginErr] = useState('');
  const [pw, setPw] = useState('');

  // 登录态探测：与原 checkAuth 语义一致
  const checkAuth = () =>
    api('/api/admin/overview')
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false));

  useEffect(() => { checkAuth(); }, []);

  const login = (password) =>
    api('/api/admin/login', { method: 'POST', body: JSON.stringify({ password }) })
      .then(() => { setLoginErr(''); return checkAuth(); })
      .catch((e) => { setLoginErr(e.message); });

  const logout = () =>
    api('/api/admin/logout', { method: 'POST' }).finally(() => setAuthed(false));

  if (!authed) {
    return (
      <main>
        <div className="card">
          <h2>登录</h2>
          <input
            type="password"
            placeholder="管理口令"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') login(pw); }}
          />
          <button className="primary" onClick={() => login(pw)}>登录</button>
          <div className="muted">{loginErr}</div>
        </div>
      </main>
    );
  }

  return (
    <>
      <header>
        <h1>CodeArts 代理管理</h1>
        <button id="logout" onClick={logout}>退出登录</button>
      </header>
      <nav>
        {TABS.map((t) => (
          <button key={t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </nav>
      <main>
        {tab === 'overview' && <OverviewTab />}
        {tab === 'models' && <ModelsTab />}
        {tab === 'benefit' && <BenefitTab />}
        {tab === 'usage' && <UsageTab />}
        {tab === 'config' && <ConfigTab />}
      </main>
    </>
  );
}
