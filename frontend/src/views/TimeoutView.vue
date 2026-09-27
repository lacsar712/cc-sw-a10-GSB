<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const role = ref(localStorage.getItem('role') || '')
const seconds = ref(null)
const bounds = ref({ min: 0.5, max: 3600 })
const secondsInput = ref(30)
const claims = ref([])
const reclaims = ref([])
const err = ref('')
const msg = ref('')
const saving = ref(false)
let timer

const isWriter = computed(() => role.value === 'writer')

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    const [s, c, r] = await Promise.all([
      api('/api/settings/claim-timeout'),
      api('/api/claims/active'),
      api('/api/reclaims'),
    ])
    seconds.value = s.seconds
    bounds.value = { min: s.min, max: s.max }
    claims.value = c
    reclaims.value = r
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function save() {
  err.value = ''
  msg.value = ''
  saving.value = true
  try {
    const data = await api('/api/settings/claim-timeout', {
      method: 'PUT',
      body: JSON.stringify({ seconds: Number(secondsInput.value) }),
    })
    seconds.value = data.seconds
    msg.value = `已保存为 ${data.seconds} 秒，仅约束之后新进领取态的单`
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    saving.value = false
  }
}

function fmtTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString()
}

function fmtRemaining(r) {
  const n = Number(r)
  if (Number.isNaN(n)) return '-'
  return n <= 0 ? '0.0（即将回收）' : n.toFixed(1)
}

onMounted(() => {
  role.value = localStorage.getItem('role') || ''
  refresh().then(() => {
    if (seconds.value !== null) secondsInput.value = seconds.value
  })
  timer = setInterval(refresh, 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <h2>领取超时台</h2>
    <p v-if="err" style="color:#b00020">{{ err }}</p>

    <section class="zone">
      <h3>一区 · 秒数设置</h3>
      <p>
        当前领取心跳超时：<strong>{{ seconds === null ? '…' : seconds + ' 秒' }}</strong>
      </p>
      <template v-if="isWriter">
        <label>
          新秒数
          <input
            type="number"
            :min="bounds.min"
            :max="bounds.max"
            step="0.5"
            v-model.number="secondsInput"
            style="width:100px"
          />
        </label>
        <button type="button" :disabled="saving" @click="save">保存</button>
        <p class="hint">改秒数只约束之后新进领取态的单；已被领取的单仍按领取时的秒数计时。</p>
        <p v-if="msg" style="color:#1a7f37">{{ msg }}</p>
      </template>
      <p v-else class="hint">巡检员只读：可查看设置与流水，不可修改秒数。</p>
    </section>

    <section class="zone">
      <h3>二区 · 监视窗（领取中 {{ claims.length }} 单）</h3>
      <table border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
        <thead>
          <tr>
            <th>编号</th><th>灯种</th><th>领取者</th><th>领取时刻</th><th>限时秒数</th><th>剩余秒数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!claims.length">
            <td colspan="6" style="text-align:center; color:#888;">暂无领取中的单</td>
          </tr>
          <tr v-for="c in claims" :key="c.id">
            <td>{{ c.id }}</td>
            <td>{{ c.lamp }}</td>
            <td>{{ c.claimed_by }}</td>
            <td>{{ fmtTime(c.claimed_at) }}</td>
            <td>{{ c.claim_timeout_seconds }}</td>
            <td :style="{ color: Number(c.remaining_seconds) <= 2 ? '#b00020' : 'inherit' }">
              {{ fmtRemaining(c.remaining_seconds) }}
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="zone">
      <h3>三区 · 回收流水（{{ reclaims.length }} 条）</h3>
      <table border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
        <thead>
          <tr>
            <th>流水号</th><th>任务号</th><th>灯种</th><th>原领取者</th><th>领取时刻</th><th>回收时刻</th><th>限时秒数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!reclaims.length">
            <td colspan="7" style="text-align:center; color:#888;">暂无回收记录</td>
          </tr>
          <tr v-for="r in reclaims" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.job_id }}</td>
            <td>{{ r.lamp }}</td>
            <td>{{ r.claimed_by }}</td>
            <td>{{ fmtTime(r.claimed_at) }}</td>
            <td>{{ fmtTime(r.reclaimed_at) }}</td>
            <td>{{ r.timeout_seconds }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.zone {
  margin: 16px 0;
  padding: 12px;
  border: 1px solid #ccc;
}
.hint {
  color: #666;
  font-size: 13px;
}
</style>
