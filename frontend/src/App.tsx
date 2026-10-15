import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ConnectionsProvider } from './components/ConnectionsProvider'
import { CountsProvider } from './components/CountsProvider'
import { Layout } from './components/Layout'
import { ProfileProvider } from './components/ProfileProvider'
import { Section } from './components/Section'
import { AnalysisPage } from './pages/AnalysisPage'
import { DashboardPage } from './pages/DashboardPage'
import { JobListPage } from './pages/JobListPage'
import { MailPage } from './pages/MailPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { ProfilePage } from './pages/ProfilePage'
import { TemplatesPage } from './pages/TemplatesPage'

// Tabs that share JobListPage get a `key` so their paging resets between tabs.
export default function App() {
  return (
    <BrowserRouter>
      <CountsProvider>
        <ProfileProvider>
          <ConnectionsProvider>
            <Routes>
              <Route element={<Layout />}>
                <Route index element={<DashboardPage />} />

                <Route path="linkedin" element={<Section platform="linkedin" />}>
                  <Route index element={<Navigate to="easy-apply" replace />} />
                  <Route
                    path="easy-apply"
                    element={
                      <JobListPage
                        key="linkedin-easy-apply"
                        title="Easy Apply"
                        platform="linkedin"
                        applyType="easy_apply"
                        columns={['job', 'location', 'experience', 'posted', 'found', 'status']}
                      />
                    }
                  />
                  <Route
                    path="google-form"
                    element={
                      <JobListPage
                        key="google-form"
                        title="Google Form"
                        applyType="google_form"
                        columns={['job', 'location', 'posted', 'open', 'status']}
                      />
                    }
                  />
                  <Route
                    path="company-website"
                    element={
                      <JobListPage
                        key="linkedin-company"
                        title="Company Website"
                        platform="linkedin"
                        applyType="company_site"
                        columns={['job', 'location', 'experience', 'posted', 'open', 'status']}
                      />
                    }
                  />
                  <Route path="mail" element={<MailPage />} />
                  {/* Old address of the Mail tab. */}
                  <Route path="outreach" element={<Navigate to="/linkedin/mail" replace />} />
                </Route>

                <Route path="naukri" element={<Section platform="naukri" />}>
                  <Route index element={<Navigate to="one-click" replace />} />
                  <Route
                    path="one-click"
                    element={
                      <JobListPage
                        key="naukri-one-click"
                        title="One-click"
                        platform="naukri"
                        applyType="one_click"
                        columns={['job', 'location', 'experience', 'posted', 'found', 'status']}
                      />
                    }
                  />
                  <Route
                    path="company-website"
                    element={
                      <JobListPage
                        key="naukri-company"
                        title="Company Website"
                        platform="naukri"
                        applyType="company_site"
                        columns={['job', 'location', 'experience', 'posted', 'open', 'status']}
                      />
                    }
                  />
                </Route>

                <Route path="templates" element={<TemplatesPage />} />
                <Route path="analysis" element={<AnalysisPage />} />
                <Route path="profile" element={<ProfilePage />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Routes>
          </ConnectionsProvider>
        </ProfileProvider>
      </CountsProvider>
    </BrowserRouter>
  )
}
