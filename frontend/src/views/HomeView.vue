<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'

const router = useRouter()
const role = ref(localStorage.getItem('role') || '')
const jobs = ref([])
const err = ref('')
const form = ref({ lamp: '', nominal_nm: 0.15, measured_nm: 0.15, simulate_stall: false })
let timer

const STATUS_TEXT = { pending: '待处理', claimed: '领取中', done: '已完成' }

function statusText(s) {
  return STATUS_TEXT[s] || s
}

function fmtTime(s) {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    jobs.value = await api('/api/jobs')
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function submit() {
  err.value = ''
  try {
    await api('/api/jobs', { method: 'POST', body: JSON.stringify(form.value) })
    form.value.lamp = ''
    form.value.simulate_stall = false
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function goDetail(id) {
  router.push(`/jobs/${id}`)
}

onMounted(() => {
  role.value = localStorage.getItem('role') || ''
  refresh()
  timer = setInterval(refresh, 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <p v-if="err" style="color:#b00020">{{ err }}</p>
    <section v-if="role === 'writer'" style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>提交校准</h3>
      <label>灯种 <input v-model="form.lamp" /></label>
      <label>标称 nm <input type="number" step="0.01" v-model.number="form.nominal_nm" /></label>
      <label>实测 nm <input type="number" step="0.01" v-model.number="form.measured_nm" /></label>
      <label title="领取进程领取后不再发心跳，超过超时台设置的秒数会被自动回收">
        <input type="checkbox" v-model="form.simulate_stall" />
        拖住领取（模拟心跳超时）
      </label>
      <button @click="submit">入队</button>
      <p class="hint">
        勾选「拖住领取」后，可到顶部「超时台」把秒数压小并观察：单子先处于领取中，超时后自动退回待处理，回收流水出现新行。
      </p>
    </section>
    <table border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
      <thead>
        <tr>
          <th>编号</th><th>灯种</th><th>标称</th><th>实测</th><th>状态</th>
          <th>领取时间</th><th>超时秒数</th><th>结论</th><th>理由</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="j in jobs"
          :key="j.id"
          style="cursor:pointer"
          @click="goDetail(j.id)"
        >
          <td>{{ j.id }}</td>
          <td>{{ j.lamp }}</td>
          <td>{{ j.nominal_nm }}</td>
          <td>{{ j.measured_nm }}</td>
          <td :class="['st-' + j.status]">
            {{ statusText(j.status) }}<span v-if="j.simulate_stall" title="心跳被拖住">⏸</span>
          </td>
          <td>{{ fmtTime(j.claimed_at) }}</td>
          <td>{{ j.timeout_seconds ? j.timeout_seconds + ' 秒' : '—' }}</td>
          <td>{{ j.verdict }}</td>
          <td>{{ j.reason }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.hint {
  color: #666;
  font-size: 13px;
}
.st-claimed {
  color: #b26a00;
  font-weight: 600;
}
.st-pending {
  color: #1a5fb4;
}
.st-done {
  color: #2a7a2a;
}
</style>
