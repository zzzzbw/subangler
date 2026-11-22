/**
 * SubAngler M3 真实浏览器界面操作验证。
 * 用 playwright-core 驱动本机 Edge，逐步操作 UI 并断言，截图存入 .ui-verify/shots。
 */
import { chromium } from 'playwright-core'
import fs from 'node:fs'
import path from 'node:path'

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const BASE = 'http://127.0.0.1:8796'
const SHOTS = path.resolve('../.ui-verify/shots')
const MEDIA = path.resolve('../.ui-verify/media').replace(/\\/g, '/')
const SRT = path.resolve('../.ui-verify/ui-upload.zh.srt')

fs.mkdirSync(SHOTS, { recursive: true })

let pass = 0, fail = 0
const results = []
function check(name, cond, detail = '') {
  const mark = cond ? 'PASS' : 'FAIL'
  console.log(`  [${mark}] ${name}${detail ? ' — ' + detail : ''}`)
  results.push({ name, ok: !!cond, detail })
  cond ? pass++ : fail++
}

const browser = await chromium.launch({
  executablePath: EDGE, headless: true,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
})
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
const page = await ctx.newPage()
const consoleErrors = []
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
page.on('pageerror', (e) => consoleErrors.push('PAGEERROR: ' + e.message))

async function shot(name) {
  await page.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: true })
}
async function goto(navLabel, expectH1) {
  await page.locator(`.nav-item:has-text("${navLabel}")`).click()
  await page.waitForTimeout(600)
  const h1 = await page.locator('.page h1').first().innerText().catch(() => '')
  check(`导航「${navLabel}」→ 标题「${expectH1}」`, h1.trim() === expectH1, `实际=${h1.trim()}`)
}

try {
  console.log('\n=== 1. 登录门禁 ===')
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1000)
  check('未登录自动跳转 /login', page.url().endsWith('/login'), page.url())

  // 错误密码
  await page.locator('input[autocomplete="current-password"]').fill('wrong-pass')
  await page.locator('button:has-text("登录")').click()
  await page.waitForTimeout(700)
  const errShown = await page.locator('.error-box').count()
  check('错误密码显示错误提示', errShown > 0, `错误框数量=${errShown}`)
  await shot('01-login-error')

  // 正确密码
  await page.locator('input[autocomplete="username"]').fill('admin')
  await page.locator('input[autocomplete="current-password"]').fill('Uiverify@2026')
  await page.locator('button:has-text("登录")').click()
  await page.waitForTimeout(1200)
  check('登录后进入总览页', page.url() === BASE + '/' && await page.locator('.app-shell').count() > 0, page.url())
  const statCount = await page.locator('.stat-card').count()
  check('总览统计卡片渲染（4 张）', statCount === 4, `实际=${statCount}`)
  await shot('02-dashboard')

  console.log('\n=== 2. 七页导航 ===')
  await goto('媒体库', '媒体库'); await shot('03-libraries')
  await goto('剧集', '剧集'); await shot('04-videos')
  await goto('任务中心', '任务中心'); await shot('05-jobs-empty')
  await goto('候选挑选', '候选挑选'); await shot('06-candidates-empty')
  await goto('字幕源', '字幕源')
  const provCount = await page.locator('.provider-card').count()
  check('字幕源卡片加载（>0）', provCount > 0, `卡片数=${provCount}`)
  await shot('07-providers')
  await goto('设置', '设置')
  const cfgFields = await page.locator('.config-field').count()
  check('设置页配置项渲染（>0）', cfgFields > 0, `字段数=${cfgFields}`)
  await shot('08-settings')
  await goto('总览', '总览')

  console.log('\n=== 3. 添加媒体库 ===')
  await page.locator('.nav-item:has-text("媒体库")').click()
  await page.waitForTimeout(400)
  await page.locator('button:has-text("添加媒体库")').click()
  await page.waitForTimeout(300)
  await page.locator('input[placeholder*="/media/tvshows"]').fill(MEDIA)
  await page.locator('input[placeholder^="选填"]').fill('UI验证库')
  await page.locator('button[type="submit"]:has-text("添加")').click()
  await page.waitForTimeout(900)
  const libText = await page.locator('.lib-list').innerText().catch(() => '')
  check('媒体库添加后列表出现「UI验证库」', libText.includes('UI验证库'), libText.slice(0, 120).replace(/\n/g, ' '))
  await shot('09-library-added')

  console.log('\n=== 4. 触发扫描任务 ===')
  await page.locator('button:has-text("立即扫描")').first().click()
  await page.waitForTimeout(1500)
  check('扫描后跳转到任务中心', page.url().includes('/jobs?job='), page.url())
  await page.waitForTimeout(2500)
  const jobStateText = await page.locator('.job-detail-head').innerText().catch(() => '')
  check('任务详情面板显示状态/进度', /\d+%/.test(jobStateText), jobStateText.replace(/\n/g, ' '))
  await shot('10-job-scan-running')

  console.log('\n=== 5. 剧集页（扫描结果）===')
  await page.locator('.nav-item:has-text("剧集")').click()
  await page.waitForTimeout(1000)
  const rows = await page.locator('.video-row:not(.video-row-head)').count()
  check('扫描后剧集行出现（>=2）', rows >= 2, `行数=${rows}`)
  await shot('11-videos-scanned')

  console.log('\n=== 6. 字幕源开关交互 ===')
  await page.locator('.nav-item:has-text("字幕源")').click()
  await page.waitForTimeout(700)
  // 按源名称定位卡片（列表按 priority 排序，避免用 first 导致重排后误判）
  const card = page.locator('.provider-card').filter({ has: page.locator('strong:text-is("SubDL")') }).first()
  const toggle = card.locator('.toggle')
  const beforeClass = await toggle.getAttribute('class')
  await toggle.click()
  await page.waitForTimeout(900)
  const card2 = page.locator('.provider-card').filter({ has: page.locator('strong:text-is("SubDL")') }).first()
  const afterClass = await card2.locator('.toggle').getAttribute('class')
  check('SubDL 字幕源开关状态切换', beforeClass !== afterClass, `${beforeClass} -> ${afterClass}`)
  // 切回原状
  await card2.locator('.toggle').click()
  await page.waitForTimeout(600)
  await shot('12-providers-toggle')

  console.log('\n=== 7. 设置页改配置 + 语言分组 ===')
  await page.locator('.nav-item:has-text("设置")').click()
  await page.waitForTimeout(700)
  // 通用分组里改一个 int 配置（token_bucket 之类），找一个 number input
  const numInputs = page.locator('.config-input[type="number"]')
  const nCount = await numInputs.count()
  if (nCount > 0) {
    const num = numInputs.first()
    const orig = await num.inputValue()
    await num.fill(String(Number(orig) + 1))
    await page.waitForTimeout(200)
    const saveBtn = page.locator('button:has-text("保存")').last()
    const saveText = await saveBtn.innerText()
    check('修改配置后保存按钮变为「保存 N 项更改」', /保存\s*\d+\s*项更改/.test(saveText), saveText)
    await saveBtn.click()
    await page.waitForTimeout(900)
    const afterText = await saveBtn.innerText()
    check('保存后按钮恢复为「已保存」', afterText.includes('已保存'), afterText)
  } else {
    check('设置页存在 int 型配置项可供修改', false, '未找到 number input')
  }
  // 切到凭证分组：未配置凭证时输入框显示「未设置」占位而非掩码，这是正确行为；验证凭证项与显示/测试按钮渲染
  await page.locator('.settings-nav button:has-text("凭证")').click()
  await page.waitForTimeout(400)
  const credFields = await page.locator('.config-field').count()
  const revealBtns = await page.locator('button:has-text("显示")').count()
  check('凭证分组渲染凭证项与显示/测试按钮', credFields >= 4 && revealBtns >= 3,
        `凭证项=${credFields}, 显示按钮=${revealBtns}`)
  await shot('13-settings-credentials')
  // 导出配置（验证按钮可用，触发下载）
  await page.locator('.settings-nav button:has-text("通用")').click()
  await page.waitForTimeout(200)
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 5000 }).catch(() => null),
    page.locator('button:has-text("导出配置")').click(),
  ])
  check('导出配置触发文件下载', !!download, download ? download.suggestedFilename() : '无下载事件')

  console.log('\n=== 8. 候选页上传本地字幕 ===')
  await page.locator('.nav-item:has-text("候选挑选")').click()
  await page.waitForTimeout(700)
  // 选择剧集
  const videoSelect = page.locator('.head-actions select').first()
  const optCount = await videoSelect.locator('option').count()
  if (optCount > 1) {
    const val = await videoSelect.locator('option').nth(1).getAttribute('value')
    await videoSelect.selectOption(val)
    await page.waitForTimeout(200)
    const fileInput = page.locator('input[type="file"]')
    await fileInput.setInputFiles(SRT)
    await page.waitForTimeout(1200)
    const bodyTxt = await page.locator('body').innerText()
    check('上传本地字幕后出现成功提示', bodyTxt.includes('本地字幕已安装'), bodyTxt.match(/本地字幕已安装[^\n]*/)?.[0] || '未找到')
  } else {
    check('候选页存在可选剧集以上传字幕', false, `option 数=${optCount}`)
  }
  await shot('14-candidates-upload')

  console.log('\n=== 9. 登出 ===')
  await page.locator('.user-chip').click()
  await page.waitForTimeout(1000)
  check('登出后回到登录页', page.url().endsWith('/login'), page.url())
  const protectedCode = await page.evaluate(async () => {
    const r = await fetch('/api/videos'); return r.status
  })
  check('登出后 /api/videos 返回 401', protectedCode === 401, `HTTP=${protectedCode}`)
  await shot('15-logged-out')

  console.log('\n=== 10. 控制台错误检查 ===')
  // 首屏未登录时对 /api/auth/me、/api/videos 的探测返回 401 属预期；EventSource 结束也非异常。
  const realErrors = consoleErrors.filter((e) =>
    !/favicon|net::ERR_ABORTED|EventSource|download/i.test(e) && !/401 \(Unauthorized\)/.test(e))
  check('无 JS 运行时错误', realErrors.length === 0, realErrors.slice(0, 3).join(' | ') || '无')

} catch (e) {
  console.error('\n[SCRIPT ERROR]', e.message)
  check('脚本执行未抛异常', false, e.message)
  await shot('99-error').catch(() => {})
} finally {
  console.log(`\n=== 界面操作验证结果：${pass} 通过 / ${fail} 失败 ===`)
  console.log('截图目录：' + SHOTS)
  fs.writeFileSync(path.join(SHOTS, 'results.json'), JSON.stringify(results, null, 2))
  await browser.close()
}
