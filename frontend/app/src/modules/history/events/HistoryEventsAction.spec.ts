// Seam: the transaction actions a TRON event (rotkibv #11) offers its parent. It is redecoded and
// deleted as a transaction, as EVM and Solana events are.
import type { HistoryEventEntry } from '@/modules/history/events/schemas';
import type { useCustomizedEventDuplicates } from '@/modules/history/events/use-customized-event-duplicates';
import type { useHistoryEventsStatus } from '@/modules/history/events/use-history-events-status';
import { HistoryEventEntryType } from '@rotki/common';
import { createMock } from '@test/utils/create-mock';
import { mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import HistoryEventsAction from '@/modules/history/events/HistoryEventsAction.vue';
import RedecodeEventsButton from '@/modules/history/events/RedecodeEventsButton.vue';

vi.mock('@/modules/history/events/use-history-events-status', () => ({
  useHistoryEventsStatus: (): ReturnType<typeof useHistoryEventsStatus> => createMock<ReturnType<typeof useHistoryEventsStatus>>({
    ethBlockEventsDecoding: computed<boolean>(() => false),
    txEventsDecoding: computed<boolean>(() => false),
  }),
}));

vi.mock('@/modules/history/events/use-customized-event-duplicates', () => ({
  useCustomizedEventDuplicates: (): ReturnType<typeof useCustomizedEventDuplicates> => createMock<ReturnType<typeof useCustomizedEventDuplicates>>({
    fixLoading: ref<boolean>(false),
    ignoreLoading: ref<boolean>(false),
  }),
}));

const txRef = 'ab'.repeat(32);

describe('historyEventsAction', () => {
  let wrapper: VueWrapper<InstanceType<typeof HistoryEventsAction>> | undefined;

  afterEach(() => {
    wrapper?.unmount();
    wrapper = undefined;
  });

  const tronEvent = (): HistoryEventEntry => createMock<HistoryEventEntry>({
    entryType: HistoryEventEntryType.TRON_EVENT,
    ignoredInAccounting: false,
    location: 'tron',
    txRef,
  });

  function createWrapper(
    event: HistoryEventEntry = tronEvent(),
    groupEvents?: HistoryEventEntry[],
  ): VueWrapper<InstanceType<typeof HistoryEventsAction>> {
    return mount(HistoryEventsAction, {
      global: {
        stubs: { RuiMenu: { template: '<div><slot name="activator" :attrs="{}" /><slot /></div>' } },
      },
      props: { event, groupEvents, loading: false },
    });
  }

  it('should redecode a tron event as its transaction', () => {
    wrapper = createWrapper();
    wrapper.findComponent(RedecodeEventsButton).vm.$emit('redecode');

    expect(wrapper.emitted('redecode')).toEqual([[{ data: { location: 'tron', txRef }, type: HistoryEventEntryType.TRON_EVENT }]]);
  });

  it('should redecode the tron transaction of a group whose header is not one', () => {
    const header = createMock<HistoryEventEntry>({ entryType: HistoryEventEntryType.HISTORY_EVENT, ignoredInAccounting: false, location: 'tron' });
    wrapper = createWrapper(header, [header, tronEvent()]);
    wrapper.findComponent(RedecodeEventsButton).vm.$emit('redecode');

    expect(wrapper.emitted('redecode')).toEqual([[{ data: { location: 'tron', txRef }, type: HistoryEventEntryType.TRON_EVENT }]]);
  });

  it('should delete the transaction of a tron event', async () => {
    wrapper = createWrapper();
    const button = wrapper.findAll('button').find(item => item.text().includes('transactions.actions.delete_transaction'));
    await button?.trigger('click');

    expect(wrapper.emitted('delete-tx')).toEqual([[{ location: 'tron', txRef }]]);
  });
});
