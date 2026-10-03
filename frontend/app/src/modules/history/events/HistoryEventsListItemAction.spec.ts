// Seam: what deleting an event asks its parent to do. The backend keeps the last event of an EVM or
// TRON transaction (rotkibv #11), so deleting the only event of one is offered as ignoring it.
import { HistoryEventEntryType } from '@rotki/common';
import { createMock } from '@test/utils/create-mock';
import { mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';
import HistoryEventsListItemAction from '@/modules/history/events/HistoryEventsListItemAction.vue';
import { HistoryEventAccountingRuleStatus, type HistoryEventEntry } from '@/modules/history/events/schemas';

function eventOf(entryType: HistoryEventEntryType, identifier: number): HistoryEventEntry {
  return createMock<HistoryEventEntry>({
    entryType,
    eventAccountingRuleStatus: HistoryEventAccountingRuleStatus.HAS_RULE,
    eventSubtype: 'none',
    identifier,
    location: 'tron',
  });
}

describe('historyEventsListItemAction', () => {
  let wrapper: VueWrapper<InstanceType<typeof HistoryEventsListItemAction>> | undefined;

  afterEach(() => {
    wrapper?.unmount();
    wrapper = undefined;
  });

  async function deleteEmitted(item: HistoryEventEntry, completeGroupEvents: HistoryEventEntry[]): Promise<unknown> {
    wrapper = mount(HistoryEventsListItemAction, {
      global: { stubs: { HistoryEventAction: true } },
      props: { completeGroupEvents, index: 0, item },
    });
    await wrapper.find('[data-testid=row-delete]').trigger('click');
    return wrapper.emitted('delete-event')?.at(-1);
  }

  it('should ignore the only event of a tron transaction instead of deleting it', async () => {
    const item = eventOf(HistoryEventEntryType.TRON_EVENT, 1);

    expect(await deleteEmitted(item, [item])).toEqual([{ event: item, type: 'ignore' }]);
  });

  it('should delete a tron event that is not the only one of its transaction', async () => {
    const item = eventOf(HistoryEventEntryType.TRON_EVENT, 1);

    expect(await deleteEmitted(item, [item, eventOf(HistoryEventEntryType.TRON_EVENT, 2)])).toEqual([{ ids: [1], type: 'delete' }]);
  });
});
