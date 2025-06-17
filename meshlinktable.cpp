/*
SILy
Copyright (C) 2024-2025 Pierre Gaufillet

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "meshlinktable.h"
#include <cstdlib>
#include <esp32-hal.h>

MeshLinkTable::MeshLinkTable(size_t capacity) : capacity_(capacity), count_(0) {
  table_ = static_cast<MeshLink *>(malloc(capacity * sizeof(MeshLink)));
}

MeshLinkTable::~MeshLinkTable() {
  if (table_)
    free(table_);
}

bool MeshLinkTable::addOrUpdate(uint16_t local, uint16_t remote, int8_t snr) {
  for (size_t i = 0; i < count_; ++i) {
    if (table_[i].localAddr == local && table_[i].remoteAddr == remote) {
      table_[i].snr = snr;
      table_[i].lastRefreshTime = millis(); // Update last refresh time
      return true;
    }
  }
  if (count_ < capacity_) {
    table_[count_++] = {local, remote, snr, millis()}; // Add new link
    return true;
  }
  return false; // Full
}

const MeshLink *MeshLinkTable::find(uint16_t local, uint16_t remote) const {
  for (size_t i = 0; i < count_; ++i) {
    if (table_[i].localAddr == local && table_[i].remoteAddr == remote) {
      return &table_[i];
    }
  }
  return nullptr;
}

bool MeshLinkTable::remove(uint16_t local, uint16_t remote) {
  for (size_t i = 0; i < count_; ++i) {
    if (table_[i].localAddr == local && table_[i].remoteAddr == remote) {
      // Replace by the last element and reduce count
      table_[i] = table_[count_ - 1];
      --count_;
      return true;
    }
  }
  return false;
}

const MeshLink *MeshLinkTable::get(size_t index) const {
  if (index >= count_)
    return nullptr;
  return &table_[index];
}

size_t MeshLinkTable::count() const { return count_; }
