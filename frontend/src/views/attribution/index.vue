<template>
  <section class="page" data-module="attribution">
    <header class="page-head">
      <div>
        <h2>归因模型管理</h2>
        <p class="page-desc">归因规则发布时生成不可变结果摘要，数据服务按摘要登记返回契约；停用规则不会改写已发布服务的字段。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记归因规则</button>
        <button class="btn" type="button" @click="exportRows">导出归因模型清单</button>
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
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>规则版本 / 摘要</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :class="{ 'row-active': selectedId === row.id }"
          @click="selectRow(row)"
        >
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            {{ row['规则版本'] ?? '—' }}
            <span v-if="row.digest_hash" class="mono"> · {{ shortHash(String(row.digest_hash)) }}</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click.stop="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click.stop="selectRow(row)">查看摘要</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无归因模型数据，可先登记归因规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条归因模型记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="selected" class="contract-panel" data-panel="attribution-digest">
      <header class="panel-head">
        <div>
          <h3>结果摘要 · {{ selected['规则编号'] }}@{{ selected['规则版本'] ?? 'v1' }}</h3>
          <p class="page-desc">摘要冻结触点权重、回溯窗口和规则版本；发布后不可修改，重复发布同一规则版本只生效一次。</p>
        </div>
        <button class="btn ghost" type="button" @click="loadDigests(selected)">刷新摘要</button>
      </header>

      <p v-if="panelMessage" class="panel-message">{{ panelMessage }}</p>

      <table class="data-table compact">
        <thead>
          <tr><th>摘要</th><th>规则版本</th><th>回溯窗口</th><th>触点权重</th><th>结果样本</th><th>发布时间</th><th>发布人</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in digests" :key="item.digest_hash">
            <td class="mono">{{ shortHash(item.digest_hash) }}</td>
            <td>{{ item.rule_ref }}</td>
            <td>{{ item.backtrack_window_days }} 天</td>
            <td>{{ weightSummary(item) }}</td>
            <td>{{ item.attributed_users }} 人 / {{ item.attributed_revenue }}</td>
            <td>{{ item.published_at }}</td>
            <td>{{ item.published_by }}</td>
          </tr>
          <tr v-if="!digests.length">
            <td colspan="7" class="empty-state">该规则还没有发布结果摘要，提交评审后再发布规则即可生成</td>
          </tr>
        </tbody>
      </table>

      <h4>下游服务契约</h4>
      <table class="data-table compact">
        <thead><tr><th>服务</th><th>版本</th><th>契约</th><th>摘要指纹</th><th>消费方</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="item in linkedContracts" :key="item.id">
            <td>{{ item.service_number }}</td>
            <td>{{ item.version }}</td>
            <td>{{ item.id }}</td>
            <td class="mono">{{ shortHash(item.digest_hash) }}</td>
            <td>{{ item.consumers.join('、') }}</td>
            <td>{{ item.status }}{{ item.current ? ' · 当前' : '' }}</td>
          </tr>
          <tr v-if="!linkedContracts.length">
            <td colspan="6" class="empty-state">该规则尚未被任何服务契约引用</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | undefined>

type Digest = {
  digest_hash: string
  rule_ref: string
  backtrack_window_days: string
  touchpoint_weights: Record<string, string>
  attributed_users: number
  attributed_revenue: string
  published_at: string
  published_by: string
}

type Contract = {
  id: number
  service_number: string
  version: string
  rule_ref: string
  digest_hash: string
  consumers: string[]
  status: string
  current: boolean
}

const ENDPOINT = '/api/attribution'
const columns = ['规则编号', '归因类型', '回溯窗口天', '触点权重', '首次触点权重', '末次触点权重', '中间触点衰减', '关联渠道']
const actions = ['提交规则评审', '发布规则', '下线规则']
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const panelMessage = ref('')
const filters = ref<Record<string, string>>({})
const selectedId = ref<number | null>(null)
const digests = ref<Digest[]>([])
const linkedContracts = ref<Contract[]>([])
const digestCount = ref(0)

const selected = ref<Row | null>(null)

const stats = [
  { label: '归因规则总数', value: 0 },
  { label: '已发布规则', value: 0 },
  { label: '已生成结果摘要', value: 0 },
]

function shortHash(value: string) {
  return value ? value.slice(0, 12) : '—'
}

function weightSummary(item: Digest) {
  const weights = item.touchpoint_weights ?? {}
  return `首次 ${weights['首次触点权重'] ?? '—'} / 末次 ${weights['末次触点权重'] ?? '—'} / 衰减 ${weights['中间触点衰减'] ?? '—'}`
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '归因规则登记入口尚未接入审批流'
}

async function selectRow(row: Row) {
  selectedId.value = Number(row.id)
  selected.value = row
  panelMessage.value = ''
  await Promise.all([loadDigests(row), loadContracts(row)])
}

async function loadDigests(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/digests?rule_number=${encodeURIComponent(String(row['规则编号']))}`)
    const payload = await response.json()
    digests.value = payload.items ?? []
    digestCount.value = payload.total ?? digests.value.length
  } catch {
    digests.value = []
  }
}

async function loadContracts(row: Row) {
  try {
    const response = await request(`/api/attribution/contracts?rule_number=${encodeURIComponent(String(row['规则编号']))}`)
    const payload = await response.json()
    linkedContracts.value = payload.items ?? []
  } catch {
    linkedContracts.value = []
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  panelMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '归因模型动作未生效')
    }
    if (action === '发布规则') {
      panelMessage.value = payload.message
    }
    await reload()
    if (selectedId.value === Number(row.id) || action === '发布规则') {
      selectedId.value = Number(row.id)
      selected.value = row
      await Promise.all([loadDigests(row), loadContracts(row)])
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归因模型操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('归因规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = total.value
    stats[1].value = rows.value.filter((row) => row.status === '已发布').length
    stats[2].value = digestCount.value
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归因模型列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  try {
    const response = await request(`${ENDPOINT}/digests`)
    const payload = await response.json()
    digestCount.value = payload.total ?? 0
    stats[2].value = digestCount.value
  } catch {
    digestCount.value = 0
  }
})
</script>

<style scoped>
.row-active { background: #eef4ff; }
.contract-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-top: 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
.panel-head h3 { margin: 0 0 4px; }
.panel-message { color: #1f6feb; font-size: 13px; margin: 6px 0 10px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
h4 { margin: 14px 0 8px; font-size: 14px; }
</style>
