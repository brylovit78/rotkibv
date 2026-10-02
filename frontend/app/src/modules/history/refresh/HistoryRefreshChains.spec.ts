// Seam: which chains the history refresh offers for a tracked account, and the accounts the
// component emits to its parent as the selection. TRON is offered as its own chain (#10).
import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { computed } from 'vue';
import HistoryRefreshChains from '@/modules/history/refresh/HistoryRefreshChains.vue';

const tronAddress = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t';

vi.mock('@/modules/core/common/use-supported-chains', () => ({
  useSupportedChains: (): object => ({
    bitcoinChainsData: computed(() => []),
    evmLikeChainsData: computed(() => []),
    solanaChainsData: computed(() => []),
    tronChainsData: computed(() => [{ id: 'tron', image: 'tron.svg', name: 'TRON', type: 'tron' }]),
    txEvmChains: computed(() => []),
  }),
}));

vi.mock('@/modules/balances/blockchain/use-account-addresses', () => ({
  useAccountAddresses: (): object => ({
    getAddresses: (chain: string): string[] => (chain === 'tron' ? [tronAddress] : []),
  }),
}));

const stubs = {
  HistoryRefreshChainItem: {
    props: ['addresses', 'item'],
    template: '<div data-testid="chain-item">{{ item.chain }}:{{ addresses.join(",") }}</div>',
  },
};

describe('historyRefreshChains', () => {
  let wrapper: ReturnType<typeof createWrapper> | undefined;

  function createWrapper(): VueWrapper<InstanceType<typeof HistoryRefreshChains>> {
    return mount(HistoryRefreshChains, {
      global: { stubs },
      props: { chain: undefined, modelValue: [], processing: false, search: '' },
    });
  }

  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = undefined;
  });

  it('should offer a tracked tron account and select it as a tron account', () => {
    wrapper = createWrapper();

    expect(wrapper.findAll('[data-testid=chain-item]').map(item => item.text())).toEqual([`tron:${tronAddress}`]);

    wrapper.vm.toggleSelectAll();

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[{ address: tronAddress, chain: 'tron' }]]);
  });
});
