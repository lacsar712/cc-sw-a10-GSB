<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'

const route = useRoute()
const router = useRouter()
const job = ref(null)
const err = ref('')

const STATUS_TEXT = { pending: '待处理', claimed: '领取中', done: '已完成' }

function statusText(s) {
  return STATUS_TEXT[s] || s
}

function fmtTime(s) {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  err.value = ''
  job.value = null
  try {
    job.value = await api(`/api/jobs/${route.params.id}`)
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
watch(() => route.params.id, load)
</script>

<template>
  <div>
    <p>
      <button type="button" @click="router.push('/')">返回总览</button>
      <button type="button" @click="router.push('/timeout')">去超时台</button>
    </p>
    <p v-if="err" style="color:#b00020">{{ err }}</p>
    <section v-if="job" style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>任务详情 #{{ job.id }}</h3>
      <p>灯种：{{ job.lamp }}</p>
      <p>标称 nm：{{ job.nominal_nm }}</p>
      <p>实测 nm：{{ job.measured_nm }}</p>
      <p>状态：{{ statusText(job.status) }}<span v-if="job.simulate_stall">（心跳被拖住）</span></p>
      <p>结论：{{ job.verdict || '—' }}</p>
      <p>理由：{{ job.reason || '—' }}</p>
      <hr />
      <p>领取时间：{{ fmtTime(job.claimed_at) }}</p>
      <p>最近心跳：{{ fmtTime(job.heartbeat_at) }}</p>
      <p>超时秒数快照：{{ job.timeout_seconds ? job.timeout_seconds + ' 秒' : '—' }}</p>
    </section>
  </div>
</template>
