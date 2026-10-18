import { type CSSProperties, type PropsWithChildren, type ReactNode, useEffect, useRef, useState } from 'react'
import { LayoutDashboard, Boxes, Wallet, ScrollText, Settings, PanelLeftClose, PanelLeftOpen, Sun, Moon, LogOut, Menu, X } from 'lucide-react'
import { cn } from '@/lib/utils'

export type TabKey = 'overview' | 'models' | 'benefit' | 'usage' | 'config'

// 仓库地址：桌面侧栏底部/移动端顶栏的 GitHub 按钮新标签页跳转
const REPO_URL = 'https://github.com/TsinHzl/codearts2cc'

// GitHub 品牌图标（octicon mark-github，lucide 已移除品牌图标故内联）
function GithubIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z" />
    </svg>
  )
}

type NavDef = {
  key: TabKey
  label: string
  icon: ReactNode
}

const navDefs: NavDef[] = [
  { key: 'overview', label: '概览', icon: <LayoutDashboard className="size-[18px]" /> },
  { key: 'models', label: '模型', icon: <Boxes className="size-[18px]" /> },
  { key: 'benefit', label: '额度', icon: <Wallet className="size-[18px]" /> },
  { key: 'usage', label: '使用日志', icon: <ScrollText className="size-[18px]" /> },
  { key: 'config', label: '配置', icon: <Settings className="size-[18px]" /> },
]

// 移动端底部导航：固定主项（与蓝本一致的做法）→ 概览/模型/额度 + 更多；使用日志/配置收进 More sheet。
const MOBILE_PRIMARY_KEYS: TabKey[] = ['overview', 'models', 'benefit']
const mobilePrimaryNav = MOBILE_PRIMARY_KEYS
  .map((key) => navDefs.find((item) => item.key === key))
  .filter((item): item is NavDef => Boolean(item))
const mobileMoreNav = navDefs.filter((item) => !MOBILE_PRIMARY_KEYS.includes(item.key))

export function useTheme() {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    if (typeof window === 'undefined') return 'light'
    return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
  })
  const toggle = () => {
    const root = document.documentElement
    const next = root.classList.contains('dark') ? 'light' : 'dark'
    root.classList.toggle('dark', next === 'dark')
    setTheme(next)
    try {
      window.localStorage.setItem('theme', next)
    } catch {
      /* localStorage 不可用时静默忽略 */
    }
  }
  return { theme, toggle }
}

type LayoutProps = PropsWithChildren<{
  active: TabKey
  onNavigate: (key: TabKey) => void
  onLogout: () => void
}>

export default function Layout({ children, active, onNavigate, onLogout }: LayoutProps) {
  const { theme, toggle } = useTheme()
  // More sheet 焦点管理：打开移焦到关闭按钮，关闭后还焦来源触发按钮。
  // 顶栏 Menu 与底部导航「更多」两个按钮同时挂载，各持独立 ref 防止还焦错位。
  const topbarMoreRef = useRef<HTMLButtonElement | null>(null)
  const bottomMoreRef = useRef<HTMLButtonElement | null>(null)
  const moreCloseRef = useRef<HTMLButtonElement | null>(null)
  const lastTriggerRef = useRef<'topbar' | 'bottom'>('bottom')
  // 侧栏折叠状态。lg+ 屏才生效；collapsed=true 时只显示 icon，列宽从 296 → 64。
  // 展开态标题区约 168px（296 − 32 内边距 − 40 logo − 32 按钮 − 24 间距），
  // 与下方 max-w-[176px] 手工耦合，调整列宽时需同步复核。
  // localStorage 持久化跨刷新保留选择（与蓝本同键 sidebar_collapsed）。
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    try {
      return window.localStorage.getItem('sidebar_collapsed') === '1'
    } catch {
      return false
    }
  })
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false)

  const toggleSidebarCollapsed = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev
      try {
        window.localStorage.setItem('sidebar_collapsed', next ? '1' : '0')
      } catch {
        /* localStorage 不可用时静默忽略 */
      }
      return next
    })
  }

  // 路由（页签）变化时关闭移动端 More sheet。
  useEffect(() => {
    setMobileMoreOpen(false)
  }, [active])

  // More sheet 打开时锁定 body 滚动，并把焦点移入对话框；关闭/卸载时还焦来源触发按钮。
  useEffect(() => {
    if (!mobileMoreOpen) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    moreCloseRef.current?.focus()
    return () => {
      document.body.style.overflow = prev
      ;(lastTriggerRef.current === 'topbar' ? topbarMoreRef : bottomMoreRef).current?.focus()
    }
  }, [mobileMoreOpen])

  useEffect(() => {
    if (!mobileMoreOpen) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileMoreOpen(false)
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [mobileMoreOpen])

  // glass 变量注入：本仓库暂无自定义背景图能力，backgroundImage 恒为 null，
  // 但保留该 effect 骨架与蓝本语义一致——有背景时按 glassOpacity 派生 13 个 glass 变量，
  // 无背景时清理。当前恒走清理分支，等价于每次挂载时移除残留变量。
  useEffect(() => {
    const root = document.documentElement
    const glassVars = [
      '--admin-glass-card-opacity',
      '--admin-glass-card-strong-opacity',
      '--admin-glass-control-opacity',
      '--admin-glass-sidebar-opacity',
      '--admin-glass-border-opacity',
      '--admin-glass-card',
      '--admin-glass-card-strong',
      '--admin-glass-control',
      '--admin-glass-sidebar',
      '--admin-glass-border',
      '--admin-glass-panel-blur',
      '--admin-glass-control-blur',
      '--admin-glass-shadow-alpha',
    ]
    const backgroundImage: string | null = null
    if (backgroundImage) {
      root.dataset.adminBackground = 'true'
      // 本仓库无 branding 配置面，以下派生公式照抄蓝本默认值路径（不可达，保留语义）
      const glassOpacity: number = 70
      const panelBlur: number = 5
      const strongOpacity = glassOpacity === 0 ? 0 : Math.min(100, glassOpacity + 8)
      const controlOpacity = Math.max(0, glassOpacity - 12)
      const sidebarOpacity = glassOpacity === 0 ? 0 : Math.min(100, glassOpacity + 6)
      const borderOpacity = glassOpacity === 0 ? 0 : Math.min(100, Math.max(38, glassOpacity + 14))
      root.style.setProperty('--admin-glass-card-opacity', `${glassOpacity}%`)
      root.style.setProperty('--admin-glass-card-strong-opacity', `${strongOpacity}%`)
      root.style.setProperty('--admin-glass-control-opacity', `${controlOpacity}%`)
      root.style.setProperty('--admin-glass-sidebar-opacity', `${sidebarOpacity}%`)
      root.style.setProperty('--admin-glass-border-opacity', `${borderOpacity}%`)
      root.style.setProperty('--admin-glass-card', glassOpacity === 0 ? 'transparent' : `color-mix(in oklab, var(--color-card) ${glassOpacity}%, transparent)`)
      root.style.setProperty('--admin-glass-card-strong', strongOpacity === 0 ? 'transparent' : `color-mix(in oklab, var(--color-card) ${strongOpacity}%, transparent)`)
      root.style.setProperty('--admin-glass-control', controlOpacity === 0 ? 'transparent' : `color-mix(in oklab, var(--color-background) ${controlOpacity}%, transparent)`)
      root.style.setProperty('--admin-glass-sidebar', sidebarOpacity === 0 ? 'transparent' : `color-mix(in oklab, hsl(var(--sidebar-background)) ${sidebarOpacity}%, transparent)`)
      root.style.setProperty('--admin-glass-border', borderOpacity === 0 ? 'transparent' : `color-mix(in oklab, var(--color-border) ${borderOpacity}%, transparent)`)
      root.style.setProperty('--admin-glass-panel-blur', `${panelBlur}px`)
      root.style.setProperty('--admin-glass-control-blur', `${Math.max(0, panelBlur - 2)}px`)
      root.style.setProperty('--admin-glass-shadow-alpha', `${Math.min(0.24, (glassOpacity / 58) * 0.14).toFixed(3)}`)
    } else {
      delete root.dataset.adminBackground
      glassVars.forEach((name) => root.style.removeProperty(name))
    }

    return () => {
      delete root.dataset.adminBackground
      glassVars.forEach((name) => root.style.removeProperty(name))
    }
  }, [])

  // Apple HIG-style easing for sidebar choreography（与蓝本一致）:
  //  - container width/padding/gap use 380ms (long, slow ease-out)
  //  - text opacity/translate use 200ms (snappy)
  //  - when expanding, text waits 150ms so the container opens up first;
  //    when collapsing, text fades out immediately
  const containerEase = 'duration-[380ms] ease-[cubic-bezier(0.32,0.72,0,1)]'
  const textEase = 'duration-[200ms] ease-[cubic-bezier(0.32,0.72,0,1)]'
  const textRevealDelay = sidebarCollapsed ? 'delay-0' : 'delay-150'

  return (
    <div className="relative min-h-dvh">
      <div
        className={`relative z-10 grid max-w-full grid-cols-1 max-lg:px-3 lg:grid-cols-[var(--admin-layout-columns)] transition-[grid-template-columns] ${containerEase}`}
        style={{
          '--admin-layout-columns': sidebarCollapsed ? '64px minmax(0,1fr)' : '296px minmax(0,1fr)',
        } as CSSProperties}
      >
        {/* Sidebar - desktop */}
        <aside data-slot="admin-sidebar" className="sticky top-0 self-start h-dvh overflow-hidden border-r border-border bg-[hsl(var(--sidebar-background))] max-lg:hidden">
          <div className={`flex flex-col h-full ${sidebarCollapsed ? 'px-2' : 'px-4'} pt-5 pb-4 transition-[padding] ${containerEase}`}>
            {/* Brand */}
            <div className={`pb-4 border-b border-border ${sidebarCollapsed ? 'flex flex-col items-center' : ''}`}>
              <div className={`flex ${sidebarCollapsed ? 'flex-col items-center gap-2' : 'w-full items-center justify-between gap-3'}`}>
                <div className="size-10 shrink-0 rounded-lg bg-primary/15 object-cover shadow-sm flex items-center justify-center">
                  <Boxes className="size-5 text-primary" />
                </div>
                <div
                  aria-hidden={sidebarCollapsed}
                  className={`min-w-0 overflow-hidden transition-[max-width,max-height] ${containerEase} ${textRevealDelay} ${
                    sidebarCollapsed ? 'pointer-events-none max-h-0 max-w-0 opacity-0' : 'max-w-[176px] opacity-100'
                  }`}
                >
                  <div
                    className={`flex min-w-0 flex-col gap-1 whitespace-nowrap transition-[opacity,transform] ${textEase} ${textRevealDelay} ${
                      sidebarCollapsed ? '-translate-x-1 opacity-0' : 'translate-x-0 opacity-100'
                    }`}
                  >
                    <h1 className="max-w-[176px] truncate text-[18px] leading-tight font-bold text-foreground" title="CodeArts2CC">
                      CodeArts2CC
                    </h1>
                  </div>
                </div>

                {/* Collapse toggle: 展开态位于 brand 行右侧，收起态位于 logo 下方 */}
                <button
                  type="button"
                  onClick={toggleSidebarCollapsed}
                  title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
                  aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
                  className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
                >
                  {sidebarCollapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
                </button>
              </div>
            </div>

            {/* Nav */}
            <nav className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pt-3 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" aria-label="Main navigation">
              {navDefs.map((item) => {
                const isActive = item.key === active
                return (
                  <button
                    key={item.key}
                    type="button"
                    title={sidebarCollapsed ? item.label : undefined}
                    onClick={() => onNavigate(item.key)}
                    className={cn(
                      'relative flex min-h-10 w-full items-center rounded-xl border text-[14px] font-semibold transition-[background-color,color,border-color,padding,gap]',
                      containerEase,
                      sidebarCollapsed ? 'justify-center px-2 py-2' : 'gap-2.5 px-3 py-2',
                      isActive
                        ? 'border-primary/20 bg-primary/10 text-primary'
                        : 'border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground',
                    )}
                  >
                    {item.icon}
                    <span
                      aria-hidden={sidebarCollapsed}
                      className={`overflow-hidden whitespace-nowrap transition-[max-width,opacity] ${textEase} ${textRevealDelay} ${
                        sidebarCollapsed ? 'max-w-0 opacity-0' : 'max-w-[160px] opacity-100'
                      }`}
                    >
                      {item.label}
                    </span>
                  </button>
                )
              })}
            </nav>

            {/* Footer */}
            <div
              className={`mt-auto border-t border-border pt-3 transition-[gap] ${containerEase} ${
                sidebarCollapsed
                  ? 'flex flex-col items-center gap-1'
                  : 'flex items-center justify-between gap-2'
              }`}
            >
              <span
                aria-hidden={sidebarCollapsed}
                className={`inline-flex items-center gap-1.5 overflow-hidden rounded-md border border-emerald-500/15 bg-[hsl(var(--success-bg))] text-[11px] font-bold text-[hsl(var(--success))] shrink-0 whitespace-nowrap transition-[max-width,opacity,padding] ${textEase} ${textRevealDelay} ${
                  sidebarCollapsed ? 'pointer-events-none max-w-0 px-0 opacity-0' : 'max-w-[120px] px-2 py-1 opacity-100'
                }`}
              >
                <span className="size-2 rounded-full bg-emerald-500 shrink-0" />
                在线
              </span>
              <div className={`flex items-center gap-0.5 ${sidebarCollapsed ? 'flex-col' : ''}`}>
                <button
                  type="button"
                  onClick={toggle}
                  className="flex items-center justify-center size-9 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/70 transition-colors duration-150"
                  title={theme === 'dark' ? '切换到浅色' : '切换到深色'}
                  aria-label={theme === 'dark' ? '切换到浅色' : '切换到深色'}
                >
                  {theme === 'dark' ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
                </button>
                <a
                  href={REPO_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-center size-9 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/70 transition-colors duration-150"
                  title="GitHub 仓库"
                  aria-label="GitHub 仓库"
                >
                  <GithubIcon className="size-[18px]" />
                </a>
                <button
                  type="button"
                  onClick={onLogout}
                  className="flex items-center justify-center size-9 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/70 transition-colors duration-150"
                  title="退出登录"
                  aria-label="退出登录"
                >
                  <LogOut className="size-[18px]" />
                </button>
              </div>
            </div>
          </div>
        </aside>

        {/* Main content */}
        <main className="min-w-0 overflow-x-clip p-5 max-lg:p-3 max-lg:pb-safe-nav max-lg:safe-px">
          {/* Mobile topbar */}
          <header
            data-slot="admin-mobile-topbar"
            className="mb-4 hidden max-lg:flex min-w-0 w-full max-w-full items-center justify-between gap-2 overflow-hidden rounded-xl border border-border bg-card/95 p-2.5 shadow-sm safe-pt"
          >
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <button
                type="button"
                ref={topbarMoreRef}
                onClick={() => { lastTriggerRef.current = 'topbar'; setMobileMoreOpen(true) }}
                aria-controls="admin-more-sheet"
                aria-haspopup="dialog"
                aria-expanded={mobileMoreOpen}
                className="flex size-10 shrink-0 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                title="更多菜单"
                aria-label="更多菜单"
              >
                <Menu className="size-5" />
              </button>
              <div className="size-8 shrink-0 rounded-[10px] bg-primary/15 flex items-center justify-center">
                <Boxes className="size-4 text-primary" />
              </div>
              <strong className="min-w-0 flex-1 truncate text-base font-semibold" title="CodeArts2CC">
                CodeArts2CC
              </strong>
            </div>
            <div className="flex shrink-0 items-center gap-0.5">
              <button
                type="button"
                onClick={toggle}
                className="flex size-9 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                title={theme === 'dark' ? '切换到浅色' : '切换到深色'}
                aria-label={theme === 'dark' ? '切换到浅色' : '切换到深色'}
              >
                {theme === 'dark' ? <Sun className="size-4" /> : <Moon className="size-4" />}
              </button>
              <a
                href={REPO_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="flex size-9 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                title="GitHub 仓库"
                aria-label="GitHub 仓库"
              >
                <GithubIcon className="size-4" />
              </a>
              <button
                type="button"
                onClick={onLogout}
                className="flex size-9 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                title="退出登录"
                aria-label="退出登录"
              >
                <LogOut className="size-4" />
              </button>
            </div>
          </header>

          <div className="mx-auto w-full max-w-[96rem]">
            <div className="min-h-full">{children}</div>
          </div>
        </main>

        {/* Mobile bottom nav — primary destinations only */}
        <nav
          data-slot="admin-mobile-nav"
          className="fixed inset-x-0 bottom-0 z-40 hidden max-lg:block px-3 pb-[max(0.75rem,env(safe-area-inset-bottom,0px))]"
          aria-label="Mobile navigation"
        >
          <div className="rounded-2xl border border-border bg-card/95 p-1.5 shadow-lg backdrop-blur-[20px] [contain:layout_paint]">
            <div className="grid grid-cols-4 gap-0.5">
              {mobilePrimaryNav.map((item) => {
                const isActive = item.key === active
                return (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => onNavigate(item.key)}
                    className={cn(
                      'flex min-h-12 flex-col items-center justify-center gap-0.5 rounded-xl border px-1 py-1.5 text-center text-[11px] font-semibold transition-colors duration-150',
                      isActive
                        ? 'border-primary/20 bg-primary/10 text-primary'
                        : 'border-transparent text-muted-foreground hover:bg-muted/50 hover:text-foreground',
                    )}
                  >
                    {item.icon}
                    <span className="w-full truncate leading-tight">{item.label}</span>
                  </button>
                )
              })}
              <button
                type="button"
                ref={bottomMoreRef}
                onClick={() => { lastTriggerRef.current = 'bottom'; setMobileMoreOpen(true) }}
                aria-controls="admin-more-sheet"
                className={cn(
                  'flex min-h-12 flex-col items-center justify-center gap-0.5 rounded-xl border px-1 py-1.5 text-center text-[11px] font-semibold transition-colors duration-150',
                  mobileMoreOpen
                    ? 'border-primary/20 bg-primary/10 text-primary'
                    : 'border-transparent text-muted-foreground hover:bg-muted/50 hover:text-foreground',
                )}
                aria-expanded={mobileMoreOpen}
                aria-haspopup="dialog"
              >
                <Menu className="size-[18px]" />
                <span className="w-full truncate leading-tight">更多</span>
              </button>
            </div>
          </div>
        </nav>

        {/* Mobile more sheet */}
        {mobileMoreOpen && (
          <div id="admin-more-sheet" className="fixed inset-0 z-[60] max-lg:block lg:hidden" role="dialog" aria-modal="true" aria-label="更多菜单">
            <button
              type="button"
              className="absolute inset-0 bg-black/45 backdrop-blur-[2px]"
              aria-label="关闭"
              onClick={() => setMobileMoreOpen(false)}
            />
            <div className="absolute inset-x-0 bottom-0 max-h-[min(85dvh,640px)] overflow-hidden rounded-t-2xl border border-border bg-card shadow-2xl safe-pb animate-in slide-in-from-bottom-4 fade-in-0 duration-200">
              <div className="mx-auto my-2.5 h-1.5 w-10 rounded-full bg-muted-foreground/30" />
              <div className="flex items-center justify-between border-b border-border px-4 pb-3">
                <div>
                  <div className="text-sm font-semibold text-foreground">更多菜单</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">使用日志与配置等次级页签</div>
                </div>
                <button
                  type="button"
                  ref={moreCloseRef}
                  onClick={() => setMobileMoreOpen(false)}
                  className="flex size-10 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                  aria-label="关闭"
                >
                  <X className="size-5" />
                </button>
              </div>
              <div className="max-h-[calc(min(85dvh,640px)-5rem)] overflow-y-auto p-3">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {mobileMoreNav.map((item) => {
                    const isActive = item.key === active
                    return (
                      <button
                        key={item.key}
                        type="button"
                        onClick={() => { setMobileMoreOpen(false); onNavigate(item.key) }}
                        className={cn(
                          'flex min-h-[72px] flex-col items-start justify-center gap-2 rounded-xl border px-3.5 py-3 text-left transition-colors',
                          isActive
                            ? 'border-primary/25 bg-primary/10 text-primary'
                            : 'border-border/80 bg-background/60 text-foreground hover:bg-muted/50',
                        )}
                      >
                        <span className={cn('flex size-9 items-center justify-center rounded-lg', isActive ? 'bg-primary/15' : 'bg-muted/70 text-muted-foreground')}>
                          {item.icon}
                        </span>
                        <span className="text-[13px] font-semibold leading-tight">{item.label}</span>
                      </button>
                    )
                  })}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
