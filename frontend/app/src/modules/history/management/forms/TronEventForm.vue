<script setup lang="ts">
import type { TronEvent } from '@/modules/history/events/schemas';
import type { StandaloneEventData } from '@/modules/history/management/forms/form-types';
import { Blockchain, HistoryEventEntryType } from '@rotki/common';
import CounterpartyInput from '@/modules/history/events/mapping/CounterpartyInput.vue';
import { useHistoryEventCounterpartyMappings } from '@/modules/history/events/mapping/use-history-event-counterparty-mappings';
import EventDateLocation from '@/modules/history/management/forms/common/EventDateLocation.vue';
import EvmLocation from '@/modules/history/management/forms/common/EvmLocation.vue';
import { EVENT_PRICE_INTENT_KEYS } from '@/modules/history/management/forms/eth-block-event-form';
import HistoryEventAssetPriceForm from '@/modules/history/management/forms/HistoryEventAssetPriceForm.vue';
import HistoryEventTypeForm from '@/modules/history/management/forms/HistoryEventTypeForm.vue';
import {
  emptyTronEventForm,
  toTronEventEditPayload,
  toTronEventPayload,
  tronEventSchema,
  tronEventStateFromEvent,
  tronEventStateFromGroup,
} from '@/modules/history/management/forms/tron-event-form';
import { useHistoryEventForm } from '@/modules/history/management/forms/use-history-event-form';
import AmountInput from '@/modules/shell/components/inputs/AmountInput.vue';
import JsonInput from '@/modules/shell/components/inputs/JsonInput.vue';

const stateUpdated = defineModel<boolean>('stateUpdated', { default: false, required: false });

const { data } = defineProps<{ data: StandaloneEventData<TronEvent> }>();

const { t } = useI18n({ useScope: 'global' });

// A TRON event is always on TRON, so the location is displayed but never edited or sent.
const location = ref<string>(Blockchain.TRON);

const { counterparties } = useHistoryEventCounterpartyMappings();

const { form, save, seed } = useHistoryEventForm({
  initial: () => emptyTronEventForm(data.nextSequenceId),
  priceIntentKeys: EVENT_PRICE_INTENT_KEYS,
  priceIntents: state => (state.priceIntent ? [state.priceIntent] : []),
  schema: computed(() => tronEventSchema(data.type === 'edit', () => get(counterparties))),
  stateUpdated,
  toEditPayload: toTronEventEditPayload,
  transform: toTronEventPayload,
});

const { state } = form;

const isInformationalEvent = computed<boolean>(() => state.eventType === 'informational');

function touchEventType(): void {
  form.touch('eventType');
  form.touch('eventSubtype');
}

watchImmediate(() => data, (data) => {
  if (data.type === 'edit') {
    seed(tronEventStateFromEvent(data.event, data.nextSequenceId), [data.event.identifier]);
    return;
  }

  seed(data.type === 'group-add'
    ? tronEventStateFromGroup(data.group, data.nextSequenceId)
    : emptyTronEventForm(data.nextSequenceId));
});

defineExpose({
  errorCount: form.errorCount,
  save,
});
</script>

<template>
  <div>
    <HistoryEventTypeForm
      v-model:event-type="state.eventType"
      v-model:event-subtype="state.eventSubtype"
      :counterparty="state.counterparty"
      :entry-type="HistoryEventEntryType.TRON_EVENT"
      :error-messages="{
        eventType: form.errors('eventType'),
        eventSubtype: form.errors('eventSubtype'),
      }"
      show-accounting-rule-link
      :dirty="stateUpdated"
      class="mb-4"
      @touch="touchEventType()"
    />

    <div class="grid md:grid-cols-2 gap-4 mb-4">
      <EventDateLocation
        v-model:timestamp="state.timestamp"
        v-model:location="location"
        class="col-span-2"
        location-disabled
        :error-messages="{
          location: form.errors('location'),
          timestamp: form.errors('timestamp'),
        }"
        @blur="form.touch($event)"
      />
    </div>

    <RuiTextField
      v-model="state.txRef"
      variant="outlined"
      color="primary"
      :disabled="data.type !== 'add'"
      data-testid="tx-ref"
      :label="t('common.tx_hash')"
      required
      :error-messages="form.errors('txRef')"
      @blur="form.touch('txRef')"
    />

    <RuiDivider class="mb-6 mt-2" />

    <!-- No location link: an asset search by chain finds no TRX or TRC20 asset yet -->
    <HistoryEventAssetPriceForm
      v-model:asset="state.asset"
      v-model:amount="state.amount"
      v-model:price-intent="state.priceIntent"
      :location="undefined"
      :error-messages="{
        amount: form.errors('amount'),
        asset: form.errors('asset'),
      }"
      :timestamp="state.timestamp"
      :no-price-fields="isInformationalEvent"
      @blur="form.touch($event)"
    />

    <RuiDivider class="mb-6 mt-2" />

    <EvmLocation
      v-model:location-label="state.locationLabel"
      v-model:address="state.address"
      :location="location"
      :error-messages="{
        locationLabel: form.errors('locationLabel'),
        address: form.errors('address'),
      }"
      @blur="form.touch($event)"
    />

    <div class="grid md:grid-cols-2 gap-4">
      <AmountInput
        v-model="state.sequenceIndex"
        variant="outlined"
        integer
        data-testid="sequence-index"
        :label="t('transactions.events.form.sequence_index.label')"
        required
        :error-messages="form.errors('sequenceIndex')"
        @blur="form.touch('sequenceIndex')"
      />
      <CounterpartyInput
        v-model="state.counterparty"
        :label="t('common.counterparty')"
        data-testid="counterparty"
        :error-messages="form.errors('counterparty')"
        @blur="form.touch('counterparty')"
      />
    </div>

    <RuiDivider class="mb-6 mt-2" />

    <RuiTextArea
      v-model="state.notes"
      prepend-icon="lu-sticky-note"
      data-testid="notes"
      variant="outlined"
      color="primary"
      max-rows="5"
      min-rows="3"
      auto-grow
      :label="t('common.notes')"
      :hint="t('transactions.events.form.notes.hint')"
      :error-messages="form.errors('notes')"
      @blur="form.touch('notes')"
    />

    <RuiDivider class="mb-2 mt-6" />

    <RuiAccordions>
      <RuiAccordion
        data-testid="tron-event-form__advance"
        header-class="py-4"
        eager
      >
        <template #header>
          {{ t('transactions.events.form.advanced') }}
        </template>
        <div class="py-2">
          <RuiTextField
            v-model="state.groupIdentifier"
            variant="outlined"
            color="primary"
            data-testid="group-identifier"
            :disabled="state.hasActualGroupIdentifier"
            :label="t('transactions.events.form.group_identifier.label')"
            :error-messages="form.errors('groupIdentifier')"
            @blur="form.touch('groupIdentifier')"
          />

          <JsonInput
            v-model="state.extraData"
            :label="t('transactions.events.form.extra_data.label')"
          />
        </div>
      </RuiAccordion>
    </RuiAccordions>
  </div>
</template>
