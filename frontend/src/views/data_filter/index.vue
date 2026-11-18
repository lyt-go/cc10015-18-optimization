<template>
  <section class="page" data-module="data_filter">
    <header class="page-head">
      <div>
        <h2>数据筛选管理</h2>
        <p class="page-desc">维护筛选规则的输入、执行约束与结果记录，为数据筛选提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记筛选规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据筛选清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据筛选数据，可先登记筛选规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记筛选规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="hierarchy-output" data-testid="hierarchy-output">
      <h3>层级联动输出（V018）</h3>
      <p class="page-desc">读取层级聚合明细表中的筛选规则版本，只输出满足当前父级条件的记录。</p>
      <form class="filter-bar" @submit.prevent="queryHierarchyOutput">
        <label class="filter-item">
          <span>父级编号</span>
          <input v-model="parentCode" data-testid="hierarchy-parent" placeholder="如 DRIL-0001" />
        </label>
        <button class="btn" data-testid="hierarchy-query" type="submit">查询联动输出</button>
      </form>
      <p v-if="hierarchyError" class="error-text">{{ hierarchyError }}</p>
      <template v-if="hierarchyResult">
        <p class="hierarchy-meta">
          父级 {{ hierarchyResult.父级编号 }} ｜ 明细规则版本：{{ hierarchyResult.筛选规则版本.join('、') || '—' }} ｜
          命中生效版本：{{ hierarchyResult.生效规则版本.join('、') || '—' }} ｜ 输出 {{ hierarchyResult.total }} 条
        </p>
        <table class="data-table">
          <thead><tr><th>批次号</th><th>父级编号</th><th>子级编号</th><th>层级路径</th><th>指标值</th><th>筛选规则版本</th></tr></thead>
          <tbody>
            <tr v-for="row in hierarchyResult.items" :key="row.id" data-testid="hierarchy-row">
              <td>{{ row.批次号 }}</td><td>{{ row.父级编号 }}</td><td>{{ row.子级编号 }}</td>
              <td>{{ row.层级路径 }}</td><td>{{ row.指标值 }}</td><td>{{ row.筛选规则版本 }}</td>
            </tr>
            <tr v-if="!hierarchyResult.items.length"><td colspan="6" class="empty-state">当前父级条件下没有满足生效规则版本的记录</td></tr>
          </tbody>
        </table>
      </template>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据筛选记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/data_filter'
const columns = ["规则编号", "数据来源", "筛选字段", "筛选算子", "筛选阈值", "逻辑组合", "输出集合", "规则版本"]
const actions = ["提交校验", "标记生效", "停用规则"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["筛选规则总数", "待校验规则", "已生效规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'data_filter.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据筛选动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据筛选操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('筛选规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据筛选列表读取失败'
  }
}

// ---------------------------------------------------------- V018 层级联动
const parentCode = ref('')
const hierarchyResult = ref<any>(null)
const hierarchyError = ref('')

async function queryHierarchyOutput() {
  hierarchyError.value = ''
  hierarchyResult.value = null
  if (!parentCode.value.trim()) {
    hierarchyError.value = '请先输入父级编号'
    return
  }
  try {
    const query = new URLSearchParams({ 父级编号: parentCode.value.trim() }).toString()
    const response = await request(`${ENDPOINT}/hierarchy_output?${query}`)
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '联动输出查询失败')
    }
    hierarchyResult.value = payload
  } catch (error) {
    hierarchyError.value = error instanceof Error ? error.message : '联动输出查询失败'
  }
}

onMounted(reload)
</script>
<style scoped>
.hierarchy-output { border-top: 1px solid var(--border); margin-top: 16px; padding-top: 12px; }
.hierarchy-output h3 { margin: 0 0 4px; font-size: 15px; }
.hierarchy-meta { font-size: 12px; color: var(--muted); margin: 8px 0; }
</style>
