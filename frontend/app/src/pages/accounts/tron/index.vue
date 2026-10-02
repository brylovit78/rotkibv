<script setup lang="ts">
import { msg } from '@/message-key';
import AccountBalancesDefaultPage from '@/modules/accounts/AccountBalancesDefaultPage.vue';

definePage({
  meta: {
    nav: { labelKey: msg.$t('navigation_menu.accounts_sub.tron'), icon: 'lu-blockchain', parent: '/accounts/', order: 35, drawer: 'accounts-tron' },
  },
});

const { t } = useI18n({ useScope: 'global' });
</script>

<template>
  <AccountBalancesDefaultPage
    category="tron"
    :title="t('navigation_menu.accounts_sub.tron')"
  />
</template>
