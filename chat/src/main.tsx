import './lib/zod-csp'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './genui/genui.css'
import './i18n'
import { applyTheme } from './lib/theme'
import App from './App.tsx'
import { AssistantEmbed } from './components/agentos/AssistantEmbed'
import { isAssistantEmbed } from './components/agentos/assistant-embed'

applyTheme()   // before first paint: no light flash in dark mode

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {isAssistantEmbed(location.search) ? <AssistantEmbed /> : <App />}
  </StrictMode>,
)
