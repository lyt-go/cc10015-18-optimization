<template>
  <section class="page" data-module="drill_down">
    <header class="page-head">
      <div><h2>多维下钻</h2><p class="page-desc">维护下钻维度的输入、执行约束与结果记录，为多维下钻提供可验证的数据工程流程。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <section class="hierarchy-panel" data-testid="hierarchy-panel">
      <header class="page-head" style="margin-top:24px">
        <div><h3>层级聚合明细（V018）</h3><p class="page-desc">上传下钻结果文件，按父级到子级关联聚合；明细表保留唯一路径，重复导入只续写缺失路径，冲突保留首个有效父级。</p></div>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="download-package" @click="downloadPackage">下载全份明细包</button>
          <label class="btn ghost" data-testid="verify-package-label">核对下载包<input type="file" accept=".zip,.csv" style="display:none" @change="verifyPackage" /></label>
        </div>
      </header>

      <form class="analysis-form" @submit.prevent="uploadHierarchy">
        <label><span>下钻结果文件（CSV）</span>
          <input type="file" accept=".csv" data-testid="hierarchy-file" @change="onFileChange" />
        </label>
        <button class="btn primary" type="submit" data-testid="hierarchy-import" :disabled="!selectedFile || importing">{{ importing ? '导入中…' : '导入并聚合' }}</button>
      </form>
      <p v-if="importMessage" class="import-message" :class="{ 'error-text': !importOk }" data-testid="import-message">{{ importMessage }}</p>
      <p v-if="verifyMessage" class="import-message" :class="{ 'error-text': !verifyOk }" data-testid="verify-message">{{ verifyMessage }}</p>
      <ul v-if="verifyChecks.length" class="verify-checks">
        <li v-for="c in verifyChecks" :key="c.核对项" :class="{ 'error-text': !c.通过 }">{{ c.通过 ? '✓' : '✗' }} {{ c.核对项 }}：{{ c.说明 }}</li>
      </ul>

      <h4 class="block-title">父级汇总<span v-if="reconcile" class="reconcile" :class="{ 'error-text': !reconcile.总计一致 }" data-testid="reconcile-flag">
        行数核对：汇总 {{ reconcile.汇总行数合计 }} / 明细 {{ reconcile.明细行数合计 }} {{ reconcile.总计一致 ? '一致' : '不一致' }}</span></h4>
      <table class="data-table">
        <thead><tr><th>父级编号</th><th>父级名称</th><th>子级行数</th><th>指标合计</th><th>最近批次</th></tr></thead>
        <tbody>
          <tr v-for="row in summaryRows" :key="row.父级编号" data-testid="summary-row">
            <td>{{ row.父级编号 }}</td><td>{{ row.父级名称 }}</td><td>{{ row.子级行数 }}</td><td>{{ row.指标合计 }}</td><td>{{ row.批次号 }}</td>
          </tr>
          <tr v-if="!summaryRows.length"><td colspan="5" class="empty-state">暂无汇总数据，请先导入下钻结果</td></tr>
        </tbody>
      </table>

      <h4 class="block-title">层级聚合明细表（唯一路径）</h4>
      <table class="data-table">
        <thead><tr><th>批次号</th><th>父级编号</th><th>子级编号</th><th>层级路径</th><th>指标值</th><th>筛选规则版本</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="row in detailRows" :key="row.id" data-testid="detail-row">
            <td>{{ row.批次号 }}</td><td>{{ row.父级编号 }}</td><td>{{ row.子级编号 }}</td><td>{{ row.层级路径 }}</td>
            <td>{{ row.指标值 }}</td><td>{{ row.筛选规则版本 || '—' }}</td><td>{{ row.状态 }}</td>
          </tr>
          <tr v-if="!detailRows.length"><td colspan="7" class="empty-state">暂无明细数据</td></tr>
        </tbody>
      </table>

      <h4 class="block-title">冲突清单（保留首个有效父级）</h4>
      <table class="data-table">
        <thead><tr><th>批次号</th><th>子级编号</th><th>保留父级</th><th>冲突路径</th><th>判定</th></tr></thead>
        <tbody>
          <tr v-for="row in conflictRows" :key="row.id" data-testid="conflict-row">
            <td>{{ row.批次号 }}</td><td>{{ row.子级编号 }}</td><td>{{ row.保留父级 }}</td><td>{{ row.冲突路径 }}</td><td>{{ row.判定 }}</td>
          </tr>
          <tr v-if="!conflictRows.length"><td colspan="5" class="empty-state">暂无冲突记录</td></tr>
        </tbody>
      </table>

      <h4 class="block-title">导入批次留档</h4>
      <table class="data-table">
        <thead><tr><th>批次号</th><th>来源文件</th><th>状态</th><th>写入</th><th>跳过</th><th>冲突</th><th>异常说明</th></tr></thead>
        <tbody>
          <tr v-for="row in batchRows" :key="row.批次号" data-testid="batch-row">
            <td>{{ row.批次号 }}</td><td>{{ row.来源文件 }}</td><td>{{ row.状态 }}</td>
            <td>{{ row.写入行数 }}</td><td>{{ row.跳过行数 }}</td><td>{{ row.冲突行数 }}</td><td>{{ row.异常说明 || '—' }}</td>
          </tr>
          <tr v-if="!batchRows.length"><td colspan="7" class="empty-state">暂无导入批次</td></tr>
        </tbody>
      </table>
    </section>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/drill_down'
const API_BASE = (import.meta as any).env?.VITE_API_BASE ?? ''
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = ["维度编号", "维度名称", "下钻层级", "基准指标", "父级维度", "子级维度", "数据来源", "刷新间隔"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["维度编号", "维度名称", "下钻层级", "基准指标", "父级维度", "子级维度", "数据来源", "刷新间隔"].slice(0, 5))
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function runAnalysis() {
  loading.value = true
  try { const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) }); const data = await res.json(); results.value = data.items || []; chartData.value = (data.items || []).map((r: any) => ({ label: Object.values(r)[0] as string, value: Object.values(r).filter(v => typeof v === 'number')[0] as number || 0 })).slice(0, 6) } catch(e) { results.value = [] }
  loading.value = false
}

// ---------------------------------------------------------- V018 层级聚合
const summaryRows = ref<any[]>([])
const detailRows = ref<any[]>([])
const conflictRows = ref<any[]>([])
const batchRows = ref<any[]>([])
const reconcile = ref<any>(null)
const selectedFile = ref<File | null>(null)
const importing = ref(false)
const importMessage = ref('')
const importOk = ref(true)
const verifyMessage = ref('')
const verifyOk = ref(true)
const verifyChecks = ref<any[]>([])

function onFileChange(event: Event) {
  selectedFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

async function refreshHierarchy() {
  try {
    const [sumRes, detRes, conRes, batRes] = await Promise.all([
      request(`${ENDPOINT}/hierarchy/summary`), request(`${ENDPOINT}/hierarchy/detail`),
      request(`${ENDPOINT}/hierarchy/conflicts`), request(`${ENDPOINT}/hierarchy/batches`),
    ])
    const sum = await sumRes.json()
    summaryRows.value = sum.items || []
    reconcile.value = sum.核对 || null
    detailRows.value = (await detRes.json()).items || []
    conflictRows.value = (await conRes.json()).items || []
    batchRows.value = (await batRes.json()).items || []
  } catch (e) { /* 面板刷新失败保留旧数据 */ }
}

async function uploadHierarchy() {
  if (!selectedFile.value) return
  importing.value = true
  importMessage.value = ''
  try {
    const form = new FormData()
    form.append('file', selectedFile.value)
    const res = await fetch(`${API_BASE}${ENDPOINT}/hierarchy/import`, { method: 'POST', body: form })
    const data = await res.json()
    importOk.value = !!data.ok
    importMessage.value = data.message || (data.ok ? '导入完成' : '导入失败')
    await Promise.all([refreshHierarchy(), fetchSummary()])
  } catch (e) {
    importOk.value = false
    importMessage.value = e instanceof Error ? e.message : '导入请求失败'
  } finally {
    importing.value = false
  }
}

function downloadPackage() {
  const link = document.createElement('a')
  link.href = `${API_BASE}${ENDPOINT}/hierarchy/package`
  link.download = 'hierarchy_package_V018.zip'
  link.click()
}

async function verifyPackage(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  verifyMessage.value = ''
  verifyChecks.value = []
  try {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`${API_BASE}${ENDPOINT}/hierarchy/verify`, { method: 'POST', body: form })
    const data = await res.json()
    verifyOk.value = !!data.ok
    verifyMessage.value = data.message || ''
    verifyChecks.value = data.checks || []
  } catch (e) {
    verifyOk.value = false
    verifyMessage.value = e instanceof Error ? e.message : '核对请求失败'
  }
}

onMounted(() => { fetchSummary(); refreshHierarchy() })
</script>
<style scoped>
.hierarchy-panel { border-top: 1px solid var(--border); margin-top: 16px; }
.block-title { margin: 16px 0 8px; font-size: 14px; }
.import-message { font-size: 13px; margin: 8px 0; }
.reconcile { font-size: 12px; color: var(--muted); margin-left: 10px; font-weight: normal; }
.verify-checks { list-style: none; padding: 0; margin: 8px 0; font-size: 12px; }
.verify-checks li { padding: 2px 0; }
</style>
