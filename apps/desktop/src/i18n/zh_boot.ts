import type { TranslationOverrides } from './define-locale'

// The boot screen's copy, composed by zh.ts.
export const zhBoot = {
  boot: {
    ready: 'Hermes 桌面版已就绪',
    desktopBootFailedWithMessage: message => `桌面启动失败：${message}`,
    steps: {
      connectingGateway: '正在连接桌面网关',
      loadingSettings: '正在加载 Hermes 设置',
      loadingSessions: '正在加载最近会话',
      retryingRemoteBackend: '正在重新连接远程 Hermes 后端…',
      startingDesktopConnection: '正在启动桌面连接',
      startingHermesDesktop: '正在启动 Hermes 桌面版…'
    },
    errors: {
      backgroundExited: 'Hermes 后台进程已退出。',
      backgroundExitedDuringStartup: 'Hermes 后台进程在启动期间退出。',
      backendStopped: '后端已停止',
      backendUpdating: '正在更新 Hermes…',
      backendUpdatingMessage: 'Hermes 正在重启以完成更新，聊天会自动重新连接。',
      desktopBootFailed: '桌面启动失败',
      gatewayConnectionLost: '与网关的连接已断开',
      gatewayConnectionLostDetail:
        'Still retrying in the background. You can keep reading and drafting — open Gateway settings if this persists.',
      gatewaySignInRequired: '需要登录网关',
      ipcBridgeUnavailable: '桌面 IPC 桥不可用。'
    },
    failure: {
      title: 'Hermes 无法启动',
      description: '后台网关没有启动。请尝试下面的恢复步骤；这里不会删除你的对话或设置。',
      remoteTitle: '需要重新登录远程网关',
      remoteDescription: '你的远程网关会话已过期。请重新登录以恢复连接。这些操作不会删除你的对话或设置。',
      retry: '重试',
      repairInstall: '修复安装',
      useLocalGateway: '使用本地网关',
      gatewaySettings: '网关设置',
      back: '返回',
      openLogs: '打开日志',
      repairHint: '修复会重新运行安装器，在新机器上可能需要几分钟。',
      bundledReinstallHint: '捆绑安装无法从应用内部自修复——重新安装 Hermes Desktop 以恢复其后端。',
      reinstallApp: '重新安装 Hermes Desktop',
      remoteSignInHint: signInLabel =>
        `先退出已保存的远程浏览器会话，然后打开${signInLabel}。也可以使用本地网关切换到随应用提供的后端。`,
      signOutAndSignIn: '退出并重新登录',
      remoteFailureHint: '在“网关设置”中检查网关 URL 和登录，或切换到本地网关。',
      cloudDownTitle: 'Nous Cloud 代理已宕机',
      cloudDownDescription:
        '此网关连接的 Nous 托管云代理正在返回服务器错误。无法在此处重启——请检查其状态、切换到本地网关或获取支持。',
      cloudDownHint: '使用下方按钮打开 Nous Portal（查看实例状态与操作）或加入 Discord 获取支持。',
      cloudDownCheckPortal: '查看 Portal 状态',
      cloudDownDiscord: '在 Discord 获取帮助',
      hideRecentLogs: '隐藏最近日志',
      showRecentLogs: '显示最近日志',
      signedInTitle: '已登录',
      signedInMessage: '正在重新连接远程网关…',
      signInIncompleteTitle: '登录未完成',
      signInIncompleteMessage: '登录窗口在认证完成前关闭。',
      signInFailed: '登录失败',
      signInToRemoteGateway: '登录远程网关',
      signInWithProvider: provider => `使用 ${provider} 登录`,
      identityProvider: '你的身份提供方'
    }
  }
} satisfies Pick<TranslationOverrides, 'boot'>
