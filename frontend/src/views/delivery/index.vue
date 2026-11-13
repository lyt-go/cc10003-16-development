<template>
  <section class="page" data-module="delivery">
    <header class="page-head">
      <div>
        <h2>数据服务管理</h2>
        <p class="page-desc">服务版本按草稿、评审、发布、下线顺序流转；发布时按归因结果摘要登记返回字段、查询参数与消费方。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记服务版本</button>
        <button class="btn" type="button" @click="exportRows">导出数据服务清单</button>
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
          <th>契约状态</th>
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
          <td>{{ row.contract_id ? `契约 ${row.contract_id}` : '未登记契约' }}</td>
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
            <button class="link" type="button" @click.stop="selectRow(row)">查看契约</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据服务数据，可先登记服务版本</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据服务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="selected" class="contract-panel" data-panel="delivery-contract">
      <header class="panel-head">
        <div>
          <h3>版本契约 · {{ selected['服务编号'] }}</h3>
          <p class="page-desc">冻结返回字段：{{ frozenFields.join('、') }}</p>
        </div>
        <div class="panel-actions">
          <label class="filter-item inline">
            <span>消费方（逗号或顿号分隔）</span>
            <input v-model="contractForm.consumers" placeholder="增长看板、渠道结算、归因复盘" />
          </label>
          <label class="filter-item inline">
            <span>归因规则</span>
            <input v-model="contractForm.ruleRef" placeholder="ATTR-0003@v6" />
          </label>
          <button class="btn" type="button" @click="previewContract">契约预览</button>
          <button class="btn primary" type="button" @click="publishContract">发布契约</button>
        </div>
      </header>

      <p v-if="panelMessage" class="panel-message">{{ panelMessage }}</p>

      <div v-if="preview" class="contract-grid">
        <article class="contract-card">
          <h4>契约预览</h4>
          <dl>
            <dt>服务版本</dt><dd>{{ preview.version }}</dd>
            <dt>规则版本</dt><dd>{{ preview.rule_ref }}</dd>
            <dt>回溯窗口</dt><dd>{{ preview.backtrack_window_days }} 天</dd>
            <dt>消费方（{{ preview.consumer_count }}）</dt><dd>{{ preview.consumers.join('、') }}</dd>
            <dt>摘要指纹</dt><dd class="mono">{{ shortHash(preview.digest_hash) }}</dd>
            <dt>预览指纹</dt><dd class="mono">{{ shortHash(preview.preview_hash) }}</dd>
          </dl>
        </article>
        <article class="contract-card">
          <h4>返回字段与来源</h4>
          <table class="data-table compact">
            <thead><tr><th>字段</th><th>来源</th></tr></thead>
            <tbody>
              <tr v-for="field in preview.return_fields" :key="field">
                <td class="mono">{{ field }}</td>
                <td>{{ preview.field_sources[field] }}</td>
              </tr>
            </tbody>
          </table>
        </article>
        <article class="contract-card">
          <h4>可回退版本</h4>
          <ul class="plain-list">
            <li v-for="item in preview.fallback_versions" :key="item.contract_id">
              契约 {{ item.contract_id }} · {{ item.version }} · {{ item.rule_ref }}
              <span class="tag" :class="{ current: item.current }">{{ item.current ? '当前' : item.status }}</span>
            </li>
            <li v-if="!preview.fallback_versions.length" class="muted">暂无历史版本</li>
          </ul>
        </article>
        <article class="contract-card">
          <h4>接口响应样本</h4>
          <pre class="sample">{{ sampleJson }}</pre>
        </article>
      </div>

      <h4>发布记录</h4>
      <table class="data-table compact">
        <thead>
          <tr><th>契约</th><th>版本</th><th>规则版本</th><th>摘要指纹</th><th>预览指纹</th><th>消费方</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in contracts" :key="item.id">
            <td>{{ item.id }}</td>
            <td>{{ item.version }}</td>
            <td class="mono">{{ item.rule_ref }}</td>
            <td class="mono">{{ shortHash(item.digest_hash) }}</td>
            <td class="mono">{{ shortHash(item.preview_hash) }}</td>
            <td>{{ item.consumers.join('、') }}</td>
            <td>{{ item.status }}{{ item.current ? ' · 当前' : '' }}</td>
          </tr>
          <tr v-if="!contracts.length">
            <td colspan="7" class="empty-state">该服务尚未发布契约</td>
          </tr>
        </tbody>
      </table>

      <h4>字段追溯</h4>
      <table class="data-table compact">
        <thead>
          <tr><th>返回字段</th><th>当前值</th><th>来源</th><th>规则版本</th><th>契约 / 摘要</th></tr>
        </thead>
        <tbody>
          <tr v-for="field in frozenFields" :key="field">
            <td class="mono">{{ field }}</td>
            <td>{{ trace[field]?.value ?? '—' }}</td>
            <td>{{ trace[field]?.source ?? '—' }}</td>
            <td class="mono">{{ trace[field]?.rule_version ?? '—' }}</td>
            <td class="mono">
              {{ trace[field] ? `契约 ${trace[field]?.contract_id} / ${shortHash(String(trace[field]?.digest_hash ?? ''))}` : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="contract-panel" data-panel="contract-audit">
      <header class="panel-head">
        <h3>契约审计</h3>
        <button class="btn ghost" type="button" @click="loadAudit">刷新审计</button>
      </header>
      <table class="data-table compact">
        <thead><tr><th>时间</th><th>动作</th><th>对象</th><th>结果</th><th>操作人</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="item in audit" :key="item.id">
            <td>{{ item.at }}</td>
            <td>{{ item.action }}</td>
            <td>{{ item.target }}</td>
            <td>{{ item.outcome }}</td>
            <td>{{ item.operator }}</td>
            <td>{{ item.detail }}</td>
          </tr>
          <tr v-if="!audit.length">
            <td colspan="6" class="empty-state">暂无审计记录</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | undefined>

type TraceItem = {
  value: string | number | null
  source: string
  rule_version: string
  contract_id: number | null
  digest_hash: string
  digest_ref: string
}

type Contract = {
  id: number
  version: string
  rule_ref: string
  digest_hash: string
  preview_hash: string
  consumers: string[]
  status: string
  current: boolean
}

type Preview = {
  version: string
  rule_ref: string
  digest_hash: string
  preview_hash: string
  backtrack_window_days: string
  attributed_users: number
  attributed_revenue: string
  consumers: string[]
  consumer_count: number
  return_fields: string[]
  field_sources: Record<string, string>
  fallback_versions: { contract_id: number; version: string; rule_ref: string; current: boolean; status: string }[]
}

type AuditItem = { id: number; at: string; action: string; target: string; outcome: string; operator: string; detail: string }

const ENDPOINT = '/api/delivery'
const frozenFields = ['rule_version', 'channel', 'first_touch_weight', 'last_touch_weight', 'middle_decay', 'attributed_value']
const columns = ['服务编号', '数据集名称', '查询参数定义', '返回字段', '速率限制', '分页最大条数', '版本号', '消费方应用']
const actions = ['提交服务评审', '发布服务', '下线服务']
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const panelMessage = ref('')
const filters = ref<Record<string, string>>({})
const selectedId = ref<number | null>(null)
const contracts = ref<Contract[]>([])
const preview = ref<Preview | null>(null)
const trace = ref<Record<string, TraceItem>>({})
const audit = ref<AuditItem[]>([])
const contractForm = ref({ ruleRef: 'ATTR-0003@v6', consumers: '增长看板、渠道结算、归因复盘' })

const selected = computed(() => rows.value.find((row) => row.id === selectedId.value) ?? null)
const sampleJson = computed(() => {
  if (!preview.value) return ''
  const sample: Record<string, unknown> = {
    attributed_users: preview.value.attributed_users,
    attributed_revenue: preview.value.attributed_revenue,
    rule_version: preview.value.rule_ref,
  }
  return JSON.stringify(sample, null, 2)
})
const stats = computed(() => [
  { label: '服务版本总数', value: total.value },
  { label: '已发布服务', value: rows.value.filter((row) => row.status === '发布').length },
  { label: '已绑定契约', value: rows.value.filter((row) => row.contract_id).length },
])

function shortHash(value: string) {
  return value ? value.slice(0, 12) : '—'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '服务版本登记入口尚未接入审批流'
}

async function selectRow(row: Row) {
  selectedId.value = Number(row.id)
  contractForm.value.ruleRef = String(row['归因规则编号'] ?? 'ATTR-0003@v6')
  if (row['消费方应用']) {
    contractForm.value.consumers = String(row['消费方应用'])
  }
  preview.value = null
  panelMessage.value = ''
  await Promise.all([loadContracts(row), loadTrace(row)])
}

async function loadContracts(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/contracts?service_number=${encodeURIComponent(String(row['服务编号']))}`)
    const payload = await response.json()
    contracts.value = payload.items ?? []
  } catch {
    contracts.value = []
  }
}

async function loadTrace(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/result/${encodeURIComponent(String(row['服务编号']))}`)
    if (!response.ok) {
      trace.value = {}
      return
    }
    const payload = await response.json()
    trace.value = payload.trace ?? {}
  } catch {
    trace.value = {}
  }
}

function contractValues() {
  return {
    版本号: String(selected.value?.['版本号'] ?? 'v1'),
    消费方应用: contractForm.value.consumers,
    归因规则编号: contractForm.value.ruleRef,
  }
}

async function previewContract() {
  if (!selected.value) return
  panelMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selected.value.id}/contract/preview`, {
      method: 'POST',
      body: JSON.stringify({ values: contractValues() }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message ?? '契约预览失败')
    preview.value = payload.preview as Preview
    panelMessage.value = payload.message
  } catch (error) {
    panelMessage.value = error instanceof Error ? error.message : '契约预览失败'
  }
}

async function publishContract() {
  if (!selected.value) return
  panelMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selected.value.id}/contract`, {
      method: 'POST',
      body: JSON.stringify({ values: contractValues() }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message ?? '契约发布失败')
    panelMessage.value = payload.message
    await Promise.all([reload(), loadContracts(selected.value), loadTrace(selected.value), loadAudit()])
  } catch (error) {
    panelMessage.value = error instanceof Error ? error.message : '契约发布失败'
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
      throw new Error('数据服务动作未生效，请稍后重试')
    }
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '数据服务动作未生效'
      await loadAudit()
      return
    }
    await reload()
    if (selectedId.value === row.id) {
      await Promise.all([loadContracts(row), loadTrace(row)])
    }
    await loadAudit()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据服务操作失败'
  }
}

async function loadAudit() {
  try {
    const response = await request(`${ENDPOINT}/audit`)
    const payload = await response.json()
    audit.value = (payload.items ?? []).slice().reverse()
  } catch {
    audit.value = []
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('服务版本列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据服务列表读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadAudit()])
})
</script>

<style scoped>
.row-active { background: #eef4ff; }
.contract-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-top: 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin-bottom: 10px; }
.panel-head h3 { margin: 0 0 4px; }
.panel-actions { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.filter-item.inline input { min-width: 220px; }
.panel-message { color: #1f6feb; font-size: 13px; margin: 6px 0 10px; }
.contract-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; }
.contract-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.contract-card h4 { margin: 0 0 8px; font-size: 14px; }
.contract-card dl { display: grid; grid-template-columns: 110px 1fr; gap: 4px 8px; margin: 0; font-size: 13px; }
.contract-card dt { color: var(--muted); }
.contract-card dd { margin: 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
.sample { margin: 0; font-size: 12px; background: #f6f8fb; padding: 8px; border-radius: 6px; overflow: auto; }
.plain-list { margin: 0; padding-left: 16px; font-size: 13px; }
.plain-list li { margin-bottom: 4px; }
.tag { font-size: 11px; border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; margin-left: 4px; }
.tag.current { border-color: var(--brand); color: var(--brand); }
.muted { color: var(--muted); }
</style>
